"""极简 MCP（Model Context Protocol）客户端：subprocess + stdio + JSON-RPC 2.0。

实现马赛克式最小协议面：

* ``initialize`` → ``notifications/initialized`` 握手；
* ``tools/list`` 列出工具；
* ``tools/call`` 调用工具（超时 10 秒）；
* 守护线程读取子进程 stdout，按 JSON-RPC ``id`` 分发响应；
* 未配置服务器时**优雅降级**：``connected=False``、``list_tools()==[]``，
  调用时抛出 :class:`MCPError`，不影响主工作流。

配置示例（``config/platform.json`` 的 ``mcp_servers``）::

    [{"name": "demo", "command": "py", "args": ["-3.14", "mcp_server.py"], "cwd": "."}]
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: MCP 协议版本（2024-11-05 为当前稳定版）。
MCP_PROTOCOL_VERSION = "2024-11-05"
#: 单次 JSON-RPC 请求的默认超时（秒）。
DEFAULT_TIMEOUT_S = 10.0


class MCPError(RuntimeError):
    """MCP 协议/传输错误。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)


@dataclass
class _Pending:
    """一次等待中的 JSON-RPC 请求。"""

    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: dict[str, Any] | None = None


class MCPClient:
    """单个 MCP 服务器的 stdio 客户端。"""

    def __init__(
        self,
        name: str = "",
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.name = str(name or "")
        self.command = str(command) if command else None
        self.args = [str(a) for a in (args or [])]
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.timeout_s = max(0.1, float(timeout_s or DEFAULT_TIMEOUT_S))
        self.connected = False
        self.last_error = ""
        self.server_info: dict[str, Any] = {}

        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail = ""
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """是否配置了启动命令。"""
        return bool(self.command)

    def connect(self, timeout_s: float | None = None) -> bool:
        """启动子进程并完成 MCP 握手；失败时返回 False 并记录 ``last_error``。"""
        if self.connected:
            return True
        if not self.configured:
            self.last_error = "未配置 MCP 服务器 command，保持未连接状态"
            return False
        timeout = max(0.1, float(timeout_s or self.timeout_s))
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [self.command or "", *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.last_error = f"MCP 服务器启动失败：{exc}"
            _LOGGER.warning("%s", self.last_error)
            self._proc = None
            return False
        self._closed = False
        self._reader = threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True
        )
        self._stderr_reader.start()
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "agentweave", "version": "1.0.0"},
                },
                timeout=timeout,
            )
            self.server_info = result if isinstance(result, dict) else {}
            self._notify("notifications/initialized", {})
            self.connected = True
            self.last_error = ""
            return True
        except (MCPError, OSError) as exc:
            self.last_error = f"MCP 握手失败：{exc}"
            _LOGGER.warning("%s", self.last_error)
            self._shutdown()
            return False

    def close(self) -> None:
        """关闭连接并回收子进程。"""
        self._shutdown()

    def _shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.connected = False
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = {"code": -32000, "message": "MCP 连接已关闭"}
            item.event.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                    pass
            # 主动关闭管道，避免解释器退出时产生 ResourceWarning
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except (OSError, ValueError):  # pragma: no cover - 读取线程可能已关闭
                    pass

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 协议方法
    # ------------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """返回服务器工具列表；未连接时返回空列表（不抛异常）。"""
        if not self.connected:
            return []
        try:
            result = self._request("tools/list", {})
        except MCPError as exc:
            self.last_error = str(exc)
            _LOGGER.warning("MCP tools/list 失败：%s", exc)
            return []
        tools = result.get("tools") if isinstance(result, dict) else None
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用服务器工具，返回 JSON-RPC ``result``（未连接时抛 MCPError）。"""
        if not self.connected:
            raise MCPError(f"MCP 服务器 {self.name or '(未命名)'} 未连接：{self.last_error or '未配置'}")
        return self._request("tools/call", {"name": str(name), "arguments": dict(arguments or {})})

    # ------------------------------------------------------------------
    # 传输层
    # ------------------------------------------------------------------
    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise MCPError(f"MCP 子进程不可用（{self.last_error or '已退出'}）")
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending()
            self._pending[request_id] = pending
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            self._send(message)
        except OSError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise MCPError(f"写入 MCP 管道失败：{exc}") from exc
        wait = max(0.1, float(timeout or self.timeout_s))
        if not pending.event.wait(wait):
            with self._lock:
                self._pending.pop(request_id, None)
            raise MCPError(f"MCP 请求超时（>{wait:g}s）：{method}")
        if pending.error:
            raise MCPError(str(pending.error.get("message", "MCP 调用失败")), int(pending.error.get("code", 0) or 0))
        return pending.result if isinstance(pending.result, dict) else {"result": pending.result}

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("MCP 管道未建立")
        data = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    _LOGGER.debug("MCP 非 JSON 输出：%s", line[:200])
                    continue
                self._dispatch(message)
        except (OSError, ValueError):  # pragma: no cover - 子进程退出时的读错误
            pass
        finally:
            with self._lock:
                self.connected = False
                pending = list(self._pending.values())
                self._pending.clear()
            for item in pending:
                item.error = {"code": -32000, "message": "MCP 子进程已退出"}
                item.event.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        if request_id is None:
            return  # 通知/日志，忽略
        with self._lock:
            pending = self._pending.pop(int(request_id), None)
        if pending is None:
            return
        if "error" in message:
            pending.error = dict(message.get("error") or {})
        else:
            pending.result = message.get("result")
        pending.event.set()

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = (line or "").strip()
                if text:
                    self._stderr_tail = (self._stderr_tail + "\n" + text)[-4000:]
        except (OSError, ValueError):  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # 信息
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """连接状态（供前端 MCP 面板展示，不含密钥）。"""
        return {
            "name": self.name,
            "connected": self.connected,
            "configured": self.configured,
            "command": self.command or "",
            "args": list(self.args),
            "protocol_version": MCP_PROTOCOL_VERSION,
            "server_info": dict(self.server_info),
            "last_error": self.last_error,
            "stderr_tail": self._stderr_tail[-500:],
        }


class MCPManager:
    """多 MCP 服务器管理器：统一连接、聚合工具、注册进 ToolRegistry。"""

    def __init__(self, servers: list[dict[str, Any]] | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self._clients: dict[str, MCPClient] = {}
        for item in servers or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            self._clients[name] = MCPClient(
                name=name,
                command=item.get("command"),
                args=item.get("args") or [],
                env=item.get("env") or {},
                cwd=item.get("cwd"),
                timeout_s=float(item.get("timeout_s") or self.timeout_s),
            )

    @property
    def clients(self) -> dict[str, MCPClient]:
        """服务器名 → 客户端。"""
        return dict(self._clients)

    def connect_all(self) -> dict[str, bool]:
        """依次连接全部服务器，返回 ``{name: connected}``（失败不影响其他）。"""
        result: dict[str, bool] = {}
        for name, client in self._clients.items():
            try:
                result[name] = client.connect()
            except Exception as exc:  # noqa: BLE001 - 单服务器异常不拖垮平台
                client.last_error = f"{type(exc).__name__}: {exc}"
                result[name] = False
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """聚合全部服务器的工具，附 ``server`` 字段。"""
        tools: list[dict[str, Any]] = []
        for name, client in self._clients.items():
            for tool in client.list_tools():
                item = dict(tool)
                item["server"] = name
                item.setdefault("name", "")
                tools.append(item)
        return tools

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用指定服务器的工具。"""
        client = self._clients.get(server)
        if client is None:
            raise MCPError(f"未知 MCP 服务器: {server}")
        return client.call_tool(tool, arguments)

    def register_into(self, registry: Any) -> int:
        """把已连接服务器的工具注册进 :class:`agentweave.tools.registry.ToolRegistry`。"""
        from ..core.types import ToolSpec

        count = 0
        for name, client in self._clients.items():
            for tool in client.list_tools():
                tool_name = str(tool.get("name") or "")
                if not tool_name:
                    continue
                spec = ToolSpec(
                    name=f"{name}.{tool_name}",
                    description=str(tool.get("description") or f"MCP 工具 {tool_name}（{name}）"),
                    parameters=dict(tool.get("inputSchema") or {"type": "object", "properties": {}}),
                    source="mcp",
                    server=name,
                    risk_level="medium",
                    timeout_s=self.timeout_s,
                    tags=["mcp", name],
                )

                def _invoke(_registry: Any = None, _doc: str = tool_name, _srv: str = name, **kwargs: Any) -> Any:
                    kwargs.pop("ctx", None)
                    return self.call_tool(_srv, _doc, kwargs)

                registry.register(spec, _invoke)
                count += 1
        return count

    def close_all(self) -> None:
        """关闭全部连接。"""
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # pragma: no cover
                _LOGGER.debug("关闭 MCP 客户端失败", exc_info=True)

    def status(self) -> list[dict[str, Any]]:
        """全部服务器状态。"""
        return [client.status() for client in self._clients.values()]

    def __len__(self) -> int:
        return len(self._clients)


def load_manager_from_config(config: dict[str, Any] | None) -> MCPManager:
    """从平台配置构造管理器（``mcp_servers`` 为空时返回空管理器）。"""
    cfg = config or {}
    servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    timeout = float(cfg.get("mcp_timeout_s", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S) if isinstance(cfg, dict) else DEFAULT_TIMEOUT_S
    return MCPManager(servers if isinstance(servers, list) else [], timeout_s=timeout)


__all__ = [
    "MCPError",
    "MCP_PROTOCOL_VERSION",
    "MCPClient",
    "MCPManager",
    "load_manager_from_config",
]
