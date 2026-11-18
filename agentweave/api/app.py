"""AgentWeave Web API 应用层：REST 路由 + SSE + 静态文件服务。

职责
----
* 按 ``docs/CONTRACTS.md`` 第 3.3 节组装 HTTP 接口（REST + SSE）；
* 把 HTTP 请求适配到平台门面 ``agentweave.runtime.platform.Platform``；
* 服务 ``web/`` 下的原生 JS/CSS 单页应用（防目录穿越）；
* 统一 JSON 错误体 ``{"error": {"code": "...", "message": "..."}}``。

设计要点
--------
* ``ThreadingHTTPServer``：每个连接独立线程，SSE 长连接不会阻塞其它请求；
* 后台 asyncio 事件循环线程：平台异步方法经 ``run_coroutine_threadsafe`` 调用；
* ``create_app(platform, config)`` 支持注入 FakePlatform，测试不依赖真实平台；
* 平台门面缺失的可选方法（如 ``get_workflow``）有降级路径，便于分模块并行开发。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import hmac
import inspect
import json
import logging
import mimetypes
import os
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from ..core.types import RunRequest

log = logging.getLogger("agentweave.api")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = REPO_ROOT / "web"

MAX_BODY_BYTES = 2 * 1024 * 1024          # POST 请求体上限 2MB
SSE_REPLAY_LIMIT = 2000                   # 单次回放的历史事件上限
MAX_EVENT_CHARS = 200_000                 # 单条 SSE 事件序列化上限（防御大对象）
RUN_RATE_WINDOW_S = 60.0                  # 限流窗口（秒）

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

MISSING = object()

#: 验证检查器说明（平台门面未提供时作为兜底；前端“验证中心”据此分层展示）
VERIFY_CHECKERS: list[dict[str, Any]] = [
    {
        "name": "schema",
        "label": "结构契约",
        "layer": "structure",
        "description": "输出必须是 JSON 对象，且包含节点声明的 outputs 契约字段。",
    },
    {
        "name": "non_empty",
        "label": "非空检查",
        "layer": "structure",
        "description": "输出非空，文本长度不少于 20 个字符。",
    },
    {
        "name": "artifact_exists",
        "label": "产物存在",
        "layer": "structure",
        "description": "声明的产物文件存在且非空。",
    },
    {
        "name": "numeric_ground_truth",
        "label": "真值比对",
        "layer": "numeric",
        "description": "用已知真值与输出 metrics 做相对误差比对（容差可配置）。",
    },
    {
        "name": "code_rerun",
        "label": "独立复算",
        "layer": "rerun",
        "description": "独立重跑输出中的 code 字段，比对重算结果与申报指标。",
    },
    {
        "name": "cross_agent",
        "label": "交叉验证",
        "layer": "cross",
        "description": "让另一个 Agent（verifier 角色）独立复算并比对结论。",
    },
    {
        "name": "llm_judge",
        "label": "语义评审",
        "layer": "cross",
        "description": "结构化 LLM 评审；离线时可降级为跳过并标注。",
    },
    {
        "name": "statistical",
        "label": "统计阈值",
        "layer": "statistical",
        "description": "样本量、拟合优度、显著性等统计阈值检查。",
    },
]

#: 五层可信验证分层（前端验证中心表格的行）
VERIFY_LAYERS: list[dict[str, Any]] = [
    {"id": "structure", "name": "结构层", "description": "输出契约与产物完整性"},
    {"id": "numeric", "name": "数值层", "description": "关键数值与真值/容差比对"},
    {"id": "rerun", "name": "复算层", "description": "独立重跑代码，验证可复现性"},
    {"id": "cross", "name": "交叉层", "description": "跨 Agent 独立复核与语义评审"},
    {"id": "statistical", "name": "统计层", "description": "样本量、拟合优度与显著性"},
]

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "web_dir": str(DEFAULT_WEB_DIR),
    "workspace_dir": str(REPO_ROOT),
    "sse_heartbeat_s": 15.0,
    "sse_replay_limit": SSE_REPLAY_LIMIT,
    "call_timeout_s": 60.0,
    "platform_start_timeout_s": 30.0,
    "agent_max_concurrency": 2,
    "auto_open_browser": False,
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """业务异常：携带 HTTP 状态码与统一错误码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


def normalize_policy_or_400(policy: str) -> str:
    """归一化调度策略；非法值直接转 400，避免脏策略静默进入运行记录。

    调度器模块缺失时原样返回（平台仍会在 start_run 做二次校验），保证
    API 层在缺模块的降级场景下可用。
    """
    try:
        from ..scheduler.engine import normalize_policy
    except ImportError:  # pragma: no cover - 调度器缺失时降级
        return str(policy or "capability")
    try:
        return normalize_policy(policy)
    except ValueError as exc:
        raise ApiError(400, "INVALID_POLICY", str(exc)) from exc


def mask_secret(value: Any) -> Any:
    """对 API Key 一类的敏感值打码，只保留首尾少量字符。"""
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 8:
        return "******"
    return f"{value[:4]}****{value[-4:]}"


def _mask_config(payload: Any) -> Any:
    """递归把配置字典里的 api_key 打码（不修改入参）。"""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key in ("api_key", "apikey", "key", "secret") and isinstance(value, str):
                out[key] = mask_secret(value)
                out.setdefault("api_key_set", bool(value))
            else:
                out[key] = _mask_config(value)
        return out
    if isinstance(payload, list):
        return [_mask_config(item) for item in payload]
    return payload


def event_to_dict(ev: Any) -> dict[str, Any]:
    """把平台事件（dict / Event dataclass / 鸭子类型对象）统一为 dict。"""
    if ev is None:
        return {}
    if isinstance(ev, dict):
        return ev
    to_dict = getattr(ev, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # pragma: no cover - 防御
            log.debug("事件 to_dict 失败", exc_info=True)
    data = {}
    for field in ("seq", "ts", "run_id", "type", "node_id", "agent_id", "payload", "level"):
        data[field] = getattr(ev, field, None)
    return data


def trim_event(ev: Any, max_chars: int = MAX_EVENT_CHARS) -> dict[str, Any]:
    """截断超大事件 payload，避免单帧 SSE 把前端卡死。"""
    data = event_to_dict(ev)
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return data
    trimmed: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > 8000:
            value = value[:8000] + f"...[truncated {len(value) - 8000} chars]"
        elif isinstance(value, (list, dict)):
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:  # pragma: no cover - 防御
                text = str(value)
            if len(text) > 8000:
                value = text[:8000] + "...[truncated]"
        trimmed[key] = value
    data = dict(data)
    data["payload"] = trimmed
    try:
        if len(json.dumps(data, ensure_ascii=False, default=str)) > max_chars:
            data["payload"] = {"_truncated": True, "type": data.get("type", "")}
    except Exception:  # pragma: no cover - 防御
        data["payload"] = {"_truncated": True}
    return data


def _event_seq(ev: dict[str, Any]) -> int:
    try:
        return int(ev.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def send_json(h: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    """发送 JSON 响应并标记响应头已发送。"""
    body = _json_bytes(payload)
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    for name, value in SECURITY_HEADERS.items():
        h.send_header(name, value)
    h.end_headers()
    h._headers_sent = True  # type: ignore[attr-defined]
    if h.command != "HEAD":
        h.wfile.write(body)
        h.wfile.flush()


def send_file(h: BaseHTTPRequestHandler, path: Path, download_name: str | None = None) -> None:
    """发送静态文件；MIME 由扩展名推断，浏览器可缓存。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ApiError(500, "FILE_READ_ERROR", f"读取文件失败：{exc}") from exc
    ctype = _guess_content_type(path)
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-cache")
    for name, value in SECURITY_HEADERS.items():
        h.send_header(name, value)
    if download_name:
        h.send_header(
            "Content-Disposition",
            "inline; filename=\"{}\"; filename*=UTF-8''{}".format(
                download_name.encode("ascii", "ignore").decode() or "artifact",
                quote(download_name),
            ),
        )
    h.end_headers()
    h._headers_sent = True  # type: ignore[attr-defined]
    if h.command != "HEAD":
        h.wfile.write(data)
        h.wfile.flush()


_MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------------------
# asyncio 桥：把平台协程安全地调度到后台事件循环线程
# ---------------------------------------------------------------------------
class AsyncBridge:
    """持有后台 asyncio 事件循环；``invoke`` 同时支持同步与异步门面方法。

    如果平台自带运行中的事件循环（``platform.loop`` / ``platform.event_loop``），
    则复用该循环，避免跨循环调用 asyncio 原语。
    """

    def __init__(self, external_loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._external = external_loop
        self._loop: asyncio.AbstractEventLoop | None = external_loop
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        if external_loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, name="agentweave-asyncio", daemon=True
            )
            self._thread.start()

    @classmethod
    def for_platform(cls, platform: Any) -> "AsyncBridge":
        loop = getattr(platform, "loop", None) or getattr(platform, "event_loop", None)
        if isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running():
            return cls(loop)
        return cls()

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:  # pragma: no cover - 进程退出路径
            try:
                loop.close()
            except Exception:
                pass

    def invoke(self, fn: Callable[..., Any], *args: Any, timeout: float = 60.0, **kwargs: Any) -> Any:
        """调用门面方法：同步直接返回，协程提交到事件循环并等待结果。"""
        result = fn(*args, **kwargs)
        if not inspect.isawaitable(result):
            return result
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("后台事件循环未就绪")
        future = asyncio.run_coroutine_threadsafe(_as_coroutine(result), loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"平台调用超时（>{timeout}s）") from exc

    def stop(self) -> None:
        """停止后台事件循环（复用平台循环时不停止它）。"""
        if self._external is not None or self._loop is None:
            return
        with self._lock:
            loop = self._loop
            self._loop = None
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:  # pragma: no cover - 已停止
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)


async def _as_coroutine(awaitable: Any) -> Any:
    return await awaitable


# ---------------------------------------------------------------------------
# WebApp：路由与业务适配
# ---------------------------------------------------------------------------
class WebApp:
    """Web 应用：持有平台门面、配置、静态目录与 asyncio 桥。"""

    def __init__(self, platform: Any, config: dict[str, Any] | None = None) -> None:
        self.platform = platform
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(config or {})
        self.config = cfg
        self.web_dir = Path(str(cfg.get("web_dir") or DEFAULT_WEB_DIR)).resolve()
        self.workspace_dir = Path(str(cfg.get("workspace_dir") or REPO_ROOT)).resolve()
        self.started_at = time.time()
        # -- 生产化：可选鉴权 / 限流 / 指标 --
        server_cfg = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
        self.auth_token = str(
            cfg.get("auth_token")
            or server_cfg.get("auth_token")
            or os.environ.get("AGENTWEAVE_AUTH_TOKEN", "")
        ).strip()
        limits = cfg.get("limits") if isinstance(cfg.get("limits"), dict) else {}
        self.run_rate_limit = max(0, int(limits.get("run_per_minute") or 0))
        self.post_rate_limit = max(0, int(limits.get("post_per_minute") or 0))
        self._rate_lock = threading.Lock()
        self._rate_buckets: dict[str, list[float]] = {}
        self.metrics: Counter = Counter()
        self.startup_error = ""
        self._shutdown_done = False
        self._routes = self._build_routes()
        self.bridge = AsyncBridge.for_platform(platform)
        self._start_platform()

    # -- 生命周期 --------------------------------------------------------
    def _start_platform(self) -> None:
        start = getattr(self.platform, "start", None)
        if not callable(start):
            return
        try:
            self.bridge.invoke(
                start, timeout=float(self.config.get("platform_start_timeout_s") or 30.0)
            )
        except Exception as exc:  # 平台启动失败不阻塞 /api/health，错误暴露在 health 中
            self.startup_error = str(exc)
            log.warning("平台启动失败：%s", exc)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        stop = getattr(self.platform, "shutdown", None)
        if callable(stop):
            try:
                self.bridge.invoke(stop, timeout=10.0)
            except Exception as exc:  # pragma: no cover - 关闭路径
                log.warning("平台关闭失败：%s", exc)
        self.bridge.stop()

    # -- 门面调用 --------------------------------------------------------
    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """调用必需的门面方法；缺失或异常统一转为 ApiError。"""
        fn = getattr(self.platform, name, None)
        if not callable(fn):
            raise ApiError(501, "NOT_IMPLEMENTED", f"平台门面尚未实现 {name}()")
        try:
            return self.bridge.invoke(
                fn, *args, timeout=float(self.config.get("call_timeout_s") or 60.0), **kwargs
            )
        except ApiError:
            raise
        except Exception as exc:
            log.exception("调用平台门面 %s 失败", name)
            raise ApiError(500, "PLATFORM_ERROR", f"平台内部错误（{name}）：{exc}") from exc

    def call_optional(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """调用可选门面方法；不存在或异常时返回 MISSING，由调用方降级。"""
        fn = getattr(self.platform, name, None)
        if not callable(fn):
            return MISSING
        try:
            return self.bridge.invoke(
                fn, *args, timeout=float(self.config.get("call_timeout_s") or 60.0), **kwargs
            )
        except Exception as exc:
            log.warning("可选门面方法 %s 调用失败：%s", name, exc)
            return MISSING

    # -- 数据读取小工具 --------------------------------------------------
    def _list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        raw = self.call_optional("list_runs", limit)
        if raw is MISSING or raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get("runs", [])
        return [item for item in raw if isinstance(item, dict)]

    def _list_agents(self) -> list[dict[str, Any]]:
        raw = self.call_optional("list_agents")
        if raw is MISSING or raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get("agents", [])
        return [item for item in raw if isinstance(item, dict)]

    def _list_tools(self) -> list[dict[str, Any]]:
        raw = self.call_optional("list_tools")
        if raw is MISSING or raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get("tools", [])
        return [item for item in raw if isinstance(item, dict)]

    def _list_workflows(self) -> list[dict[str, Any]]:
        raw = self.call_optional("list_workflows")
        if raw is MISSING or raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get("workflows", [])
        return [item for item in raw if isinstance(item, dict)]

    def _get_run(self, run_id: str) -> dict[str, Any] | None:
        raw = self.call("get_run", run_id)
        if raw is None:
            return None
        if isinstance(raw, dict) and "run_id" not in raw and isinstance(raw.get("run"), dict):
            return raw["run"]
        if isinstance(raw, dict):
            return raw
        return None

    def _get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        fn = getattr(self.platform, "get_workflow", None)
        if callable(fn):
            raw = self.call_optional("get_workflow", workflow_id)
            if isinstance(raw, dict) and raw.get("id") == workflow_id:
                return raw
            if isinstance(raw, dict) and raw.get("workflow"):
                inner = raw["workflow"]
                if isinstance(inner, dict):
                    return inner
        for wf in self._list_workflows():
            if str(wf.get("id")) == workflow_id:
                if wf.get("nodes"):
                    return wf
        return self._load_workflow_from_disk(workflow_id)

    def _load_workflow_from_disk(self, workflow_id: str) -> dict[str, Any] | None:
        """降级：平台未提供 get_workflow 时，直接读取仓库 workflows/ 目录。"""
        root = self.workspace_dir / "workflows"
        if not root.is_dir():
            return None
        candidates = sorted(root.glob("*.json")) + sorted(root.glob("*/workflow.json"))
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("id")) == workflow_id:
                return data
        return None

    def _events_total(self) -> int | None:
        for name in ("event_stats", "events_stats"):
            raw = self.call_optional(name)
            if isinstance(raw, dict):
                for key in ("events", "total", "count"):
                    if isinstance(raw.get(key), (int, float)):
                        return int(raw[key])
        bus = getattr(self.platform, "bus", None)
        stats = getattr(bus, "stats", None)
        if callable(stats):
            try:
                data = stats()
                if isinstance(data, dict) and isinstance(data.get("events"), (int, float)):
                    return int(data["events"])
            except Exception:  # pragma: no cover - 防御
                pass
        return None

    # ------------------------------------------------------------------
    # 路由表
    # ------------------------------------------------------------------
    def _build_routes(self) -> list[tuple[set[str], re.Pattern[str], Callable[..., Any]]]:
        R = re.compile
        return [
            ({"GET"}, R(r"/api/health"), self.api_health),
            ({"GET"}, R(r"/api/metrics"), self.api_metrics),
            ({"GET"}, R(r"/api/overview"), self.api_overview),
            ({"GET"}, R(r"/api/agents"), self.api_agents),
            ({"POST"}, R(r"/api/agents"), self.api_agent_save),
            ({"GET"}, R(r"/api/workflows"), self.api_workflows),
            ({"GET"}, R(r"/api/scenarios"), self.api_scenarios),
            ({"POST"}, R(r"/api/workflows"), self.api_workflow_save),
            ({"GET"}, R(r"/api/workflows/(?P<workflow_id>[^/]+)"), self.api_workflow_detail),
            ({"GET"}, R(r"/api/tools"), self.api_tools),
            ({"GET"}, R(r"/api/mcp/servers"), self.api_mcp_list),
            ({"POST"}, R(r"/api/mcp/servers"), self.api_mcp_add),
            ({"POST"}, R(r"/api/runs"), self.api_run_start),
            ({"GET"}, R(r"/api/runs"), self.api_runs),
            ({"POST"}, R(r"/api/demo/run"), self.api_demo_run),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)/events"), self.api_run_events),
            ({"POST"}, R(r"/api/runs/(?P<run_id>[^/]+)/cancel"), self.api_run_cancel),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)/artifacts"), self.api_run_artifacts),
            (
                {"GET"},
                R(r"/api/runs/(?P<run_id>[^/]+)/artifacts/(?P<name>[^/]+)"),
                self.api_artifact_download,
            ),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)/verification"), self.api_run_verification),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)/report"), self.api_run_report),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)/loop_status"), self.api_run_loop),
            ({"GET"}, R(r"/api/runs/(?P<run_id>[^/]+)"), self.api_run_detail),
            ({"GET"}, R(r"/api/verify/strategies"), self.api_verify_strategies),
            ({"GET"}, R(r"/api/config/llm"), self.api_llm_config),
            ({"POST"}, R(r"/api/config/llm"), self.api_llm_config_save),
            ({"GET"}, R(r"/api/settings/llm"), self.api_llm_config),
            ({"POST"}, R(r"/api/settings/llm"), self.api_llm_config_save),
            ({"GET"}, R(r"/api/stats/timeseries"), self.api_stats_timeseries),
        ]

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    def dispatch(self, h: BaseHTTPRequestHandler, method: str, raw_path: str) -> None:
        split = urlsplit(raw_path)
        path = unquote(split.path or "/")
        query = parse_qs(split.query, keep_blank_values=True)
        started = time.perf_counter()
        self.metrics["requests"] += 1
        try:
            if path.startswith("/api"):
                self._authorize(h, method, path)
                self._rate_limit(h, method, path)
                self.dispatch_api(h, method, path, query)
            else:
                self.serve_static(h, path)
        except ApiError:
            self.metrics["errors"] += 1
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.info("access %s %s %.1fms", method, path, elapsed_ms)

    # -- 生产化：鉴权 / 限流 -------------------------------------------
    def _authorize(self, h: BaseHTTPRequestHandler, method: str, path: str) -> None:
        # 可选 Bearer 鉴权：只保护写接口；读接口留给网络层/网关控制
        if not self.auth_token or method in ("GET", "HEAD") or path == "/api/health":
            return
        header = h.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            try:
                token = (parse_qs(urlsplit(h.path).query).get("token") or [""])[0]
            except Exception:  # pragma: no cover - 防御
                token = ""
        if not token or not hmac.compare_digest(token, self.auth_token):
            self.metrics["unauthorized"] += 1
            raise ApiError(
                401,
                "UNAUTHORIZED",
                "需要有效的 Bearer Token（Authorization: Bearer <token>）",
            )

    def _rate_limit(self, h: BaseHTTPRequestHandler, method: str, path: str) -> None:
        # 简单令牌桶：每 IP 每分钟写请求上限；运行类接口单独限流
        if method != "POST":
            return
        self.metrics["post"] += 1
        is_run = path in ("/api/runs", "/api/demo/run")
        limit = self.run_rate_limit if is_run else self.post_rate_limit
        if limit <= 0:
            return
        client_ip = "unknown"
        try:
            client_ip = str(h.client_address[0])
        except Exception:  # pragma: no cover - 防御
            pass
        key = f"{client_ip}:{'run' if is_run else 'post'}"
        now = time.time()
        with self._rate_lock:
            bucket = self._rate_buckets.setdefault(key, [])
            bucket[:] = [ts for ts in bucket if now - ts < RUN_RATE_WINDOW_S]
            if len(bucket) >= limit:
                self.metrics["rate_limited"] += 1
                raise ApiError(
                    429, "RATE_LIMITED", f"请求过于频繁（上限 {limit} 次/分钟），请稍后再试"
                )
            bucket.append(now)

    def api_metrics(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        snapshot: Any = {}
        for name in ("metrics_snapshot", "snapshot"):
            fn = getattr(self.platform, name, None)
            if callable(fn):
                try:
                    snapshot = self.bridge.invoke(fn, timeout=5.0) or {}
                except Exception as exc:  # pragma: no cover - 降级
                    snapshot = {"error": str(exc)}
                break
        runs = self._list_runs(200)
        by_status: Counter = Counter(str(run.get("status") or "unknown") for run in runs)
        send_json(
            h,
            {
                "uptime_s": self._uptime_s(),
                "requests": {
                    "total": int(self.metrics.get("requests", 0)),
                    "errors": int(self.metrics.get("errors", 0)),
                    "unauthorized": int(self.metrics.get("unauthorized", 0)),
                    "rate_limited": int(self.metrics.get("rate_limited", 0)),
                    "post": int(self.metrics.get("post", 0)),
                },
                "runs": {
                    "known": len(runs),
                    "by_status": dict(by_status),
                    "succeeded": by_status.get("succeeded", 0),
                    "failed": by_status.get("failed", 0),
                },
                "auth_enabled": bool(self.auth_token),
                "limits": {
                    "run_per_minute": self.run_rate_limit,
                    "post_per_minute": self.post_rate_limit,
                },
                "platform": snapshot,
            },
        )

    def dispatch_api(
        self,
        h: BaseHTTPRequestHandler,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> None:
        allowed: set[str] = set()
        for methods, pattern, handler in self._routes:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            allowed.update(methods)
            if method not in methods:
                continue
            body = self._read_body(h) if method == "POST" else {}
            handler(h, match, body, query)
            return
        if allowed:
            raise ApiError(
                405,
                "METHOD_NOT_ALLOWED",
                f"接口 {path} 不支持 {method} 方法（允许：{', '.join(sorted(allowed))}）",
            )
        raise ApiError(404, "NOT_FOUND", f"接口不存在：{path}")

    def _read_body(self, h: BaseHTTPRequestHandler) -> dict[str, Any]:
        raw_length = h.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except ValueError:
            raise ApiError(400, "BAD_LENGTH", "Content-Length 不合法")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(413, "BODY_TOO_LARGE", "请求体过大（上限 2MB）")
        if length == 0:
            return {}
        data = h.rfile.read(length)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(400, "BAD_ENCODING", "请求体不是合法的 UTF-8") from exc
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ApiError(400, "INVALID_JSON", f"请求体不是合法 JSON：{exc}") from exc
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ApiError(400, "INVALID_BODY", "请求体必须是 JSON 对象")
        return parsed

    # ------------------------------------------------------------------
    # 静态文件
    # ------------------------------------------------------------------
    def serve_static(self, h: BaseHTTPRequestHandler, path: str) -> None:
        """服务 web/ 目录；路径解析后必须仍在 web 根目录内（防目录穿越）。"""
        rel = path.lstrip("/") or "index.html"
        rel = rel.replace("\\", "/")
        if "\x00" in rel:
            raise ApiError(400, "BAD_PATH", "路径不合法")
        candidate = (self.web_dir / rel).resolve()
        try:
            candidate.relative_to(self.web_dir)
        except ValueError:
            raise ApiError(404, "NOT_FOUND", "资源不存在")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if candidate.is_file():
            send_file(h, candidate)
            return
        # 未知的无扩展名路径交给单页应用（前端使用 hash 路由，这里只做兜底）
        if "." not in Path(rel).name:
            index = self.web_dir / "index.html"
            if index.is_file():
                send_file(h, index)
                return
        raise ApiError(404, "NOT_FOUND", f"资源不存在：/{rel}")

    # ------------------------------------------------------------------
    # 系统 / 概览
    # ------------------------------------------------------------------
    def _uptime_s(self) -> float:
        """服务已运行秒数（下限 0.05，避免刚启动时展示为 0）。"""
        return round(max(time.time() - self.started_at, 0.05), 2)

    def api_health(self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any) -> None:
        llm: Any = {}
        llm_fn = getattr(self.platform, "llm_status", None)
        if callable(llm_fn):
            try:
                llm = self.bridge.invoke(llm_fn, timeout=10.0) or {}
            except Exception as exc:  # pragma: no cover - 降级
                llm = {"error": str(exc)}
        agents = self._list_agents()
        payload = {
            "status": "ok",
            "version": __version__,
            "uptime_s": self._uptime_s(),
            "llm": _mask_config(llm),
            "agents": len(agents),
            "platform": {
                "ready": not bool(self.startup_error),
                "error": self.startup_error,
            },
            "scheduler": self._scheduler_status(),
        }
        send_json(h, payload)

    def _scheduler_status(self) -> dict[str, Any]:
        raw = self.call_optional("scheduler_status")
        if isinstance(raw, dict):
            return raw
        scheduler = getattr(self.platform, "scheduler", None)
        describe = getattr(scheduler, "describe", None)
        if callable(describe):
            try:
                data = describe()
                if isinstance(data, dict):
                    return data
            except Exception:  # pragma: no cover - 防御
                pass
        return {}

    @staticmethod
    def _run_confidence(run: dict[str, Any]) -> float | None:
        metrics = run.get("metrics") or {}
        for key in ("avg_confidence", "confidence"):
            value = metrics.get(key) if isinstance(metrics, dict) else None
            if isinstance(value, (int, float)):
                return float(value)
        verification = run.get("verification") or []
        if isinstance(verification, list) and verification:
            last = verification[-1]
            if isinstance(last, dict) and isinstance(last.get("confidence"), (int, float)):
                return float(last["confidence"])
        nodes = run.get("nodes") or {}
        confidences = [
            item.get("confidence")
            for item in nodes.values()
            if isinstance(item, dict) and isinstance(item.get("confidence"), (int, float))
        ]
        if confidences:
            return sum(confidences) / len(confidences)
        return None

    @staticmethod
    def _run_duration(run: dict[str, Any]) -> float | None:
        metrics = run.get("metrics") or {}
        if isinstance(metrics, dict):
            value = metrics.get("duration_s")
            if isinstance(value, (int, float)):
                return float(value)
        started = run.get("started_at") or 0
        finished = run.get("finished_at") or 0
        if started and finished:
            return max(0.0, float(finished) - float(started))
        return None

    @staticmethod
    def _run_tokens(run: dict[str, Any]) -> int:
        metrics = run.get("metrics") or {}
        if isinstance(metrics, dict):
            for key in ("tokens_total", "tokens"):
                value = metrics.get(key)
                if isinstance(value, (int, float)):
                    return int(value)
        total = 0
        for item in (run.get("nodes") or {}).values():
            if isinstance(item, dict):
                total += int(item.get("tokens_in") or 0) + int(item.get("tokens_out") or 0)
        return total

    def api_overview(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        runs = self._list_runs(200)
        agents = self._list_agents()
        tools = self._list_tools()
        workflows = self._list_workflows()
        finished = [
            run
            for run in runs
            if str(run.get("status")) in ("succeeded", "failed", "cancelled", "rejected")
        ]
        succeeded = [run for run in finished if run.get("status") == "succeeded"]
        durations = [d for d in (self._run_duration(run) for run in finished) if d is not None]
        confidences = [c for c in (self._run_confidence(run) for run in finished) if c is not None]
        running = [
            run for run in runs if str(run.get("status")) in ("pending", "running", "paused")
        ]
        agent_load = self._call_optional_dict_list("agent_load")
        if not agent_load:
            inflight: Counter[str] = Counter()
            for run in running:
                for node in (run.get("nodes") or {}).values():
                    if (
                        isinstance(node, dict)
                        and node.get("agent_id")
                        and str(node.get("status")) in ("scheduled", "running", "retrying")
                    ):
                        inflight[str(node["agent_id"])] += 1
            max_conc = max(1, int(self.config.get("agent_max_concurrency") or 2))
            agent_load = [
                {
                    "agent_id": str(agent.get("id")),
                    "inflight": int(inflight.get(str(agent.get("id")), 0)),
                    "utilization": round(
                        min(1.0, inflight.get(str(agent.get("id")), 0) / max_conc), 2
                    ),
                }
                for agent in agents
            ]
        recent_events = self._call_optional_dict_list("recent_events", 30)
        if not recent_events:
            recent_events = self._recent_events_from_runs(runs)
        payload = {
            "runs": len(runs),
            "run_count": len(runs),
            "agents": len(agents),
            "agent_count": len(agents),
            "tools": len(tools),
            "tool_count": len(tools),
            "workflows": len(workflows),
            "running": len(running),
            "events": self._events_total(),
            "success_rate": round(len(succeeded) / len(finished), 3) if finished else 0.0,
            "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
            "avg_tokens": round(
                sum(self._run_tokens(run) for run in finished) / len(finished), 1
            )
            if finished
            else 0.0,
            "recent_runs": runs[:10],
            "recent_events": recent_events[:30],
            "agent_load": agent_load,
            "scheduler": self._scheduler_status(),
            "uptime_s": self._uptime_s(),
            "llm": _mask_config(self._llm_status_raw()),
        }
        send_json(h, payload)

    def _call_optional_dict_list(self, name: str, *args: Any) -> list[dict[str, Any]]:
        raw = self.call_optional(name, *args)
        if raw is MISSING or raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get(name, raw.get("items", []))
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    def _recent_events_from_runs(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for run in runs[:3]:
            run_id = run.get("run_id")
            if not run_id:
                continue
            try:
                raw = self.call("get_events", str(run_id), 0, 14)
            except ApiError:
                continue
            if raw:
                events.extend(trim_event(ev) for ev in raw)
        events.sort(key=lambda ev: (ev.get("ts") or 0), reverse=True)
        return events[:30]

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------
    def api_agents(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        raw = self.call_optional("list_agents")
        agents: list[dict[str, Any]] = []
        load: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            maybe_agents = raw.get("agents", [])
            if isinstance(maybe_agents, list):
                agents = [item for item in maybe_agents if isinstance(item, dict)]
            maybe_load = raw.get("load", [])
            if isinstance(maybe_load, list):
                load = [item for item in maybe_load if isinstance(item, dict)]
        elif isinstance(raw, list):
            agents = [item for item in raw if isinstance(item, dict)]
        if not load:
            load = self._call_optional_dict_list("agent_load")
        send_json(h, {"agents": agents, "load": load})

    def api_agent_save(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        if not body.get("id"):
            raise ApiError(400, "INVALID_AGENT", "Agent 定义缺少 id 字段")
        result: Any = MISSING
        for name in ("save_agent", "register_agent", "upsert_agent"):
            fn = getattr(self.platform, name, None)
            if callable(fn):
                result = self.call(name, body)
                break
        if result is MISSING:
            registry = getattr(self.platform, "agents", None)
            register = getattr(registry, "register", None)
            if callable(register):
                try:
                    from ..core.types import AgentSpec

                    spec = AgentSpec.from_dict(body)
                    result = self.bridge.invoke(register, spec)
                except Exception as exc:
                    raise ApiError(400, "INVALID_AGENT", f"Agent 定义不合法：{exc}") from exc
            else:
                raise ApiError(501, "NOT_IMPLEMENTED", "平台门面暂不支持保存 Agent")
        if isinstance(result, dict) and "agent" in result:
            send_json(h, result)
            return
        send_json(h, {"agent": result if isinstance(result, dict) else body})

    # ------------------------------------------------------------------
    # 工作流
    # ------------------------------------------------------------------
    def api_workflows(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        workflows = []
        for wf in self._list_workflows():
            node_count = wf.get("node_count")
            if node_count is None and isinstance(wf.get("nodes"), list):
                node_count = len(wf["nodes"])
            workflows.append(
                {
                    "id": wf.get("id"),
                    "name": wf.get("name") or wf.get("id"),
                    "version": wf.get("version", "1.0.0"),
                    "description": wf.get("description", ""),
                    "node_count": int(node_count or 0),
                    "tags": wf.get("tags", []),
                    "owner": wf.get("owner", ""),
                    "nodes": wf.get("nodes", []),
                    "edges": wf.get("edges", []),
                    "variables": wf.get("variables", {}),
                }
            )
        send_json(h, {"workflows": workflows})

    def api_workflow_detail(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        workflow_id = m.group("workflow_id")
        wf = self._get_workflow(unquote(workflow_id))
        if wf is None:
            raise ApiError(404, "WORKFLOW_NOT_FOUND", f"工作流不存在：{workflow_id}")
        send_json(h, wf)

    def api_workflow_save(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        if not body.get("id"):
            raise ApiError(400, "INVALID_WORKFLOW", "工作流缺少 id 字段")
        if not isinstance(body.get("nodes", []), list):
            raise ApiError(400, "INVALID_WORKFLOW", "工作流 nodes 必须是数组")
        result = self.call("save_workflow", body)
        send_json(h, {"workflow": result if isinstance(result, dict) else body, "saved": True})

    # ------------------------------------------------------------------
    # 工具 / MCP
    # ------------------------------------------------------------------
    def api_tools(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        raw = self.call_optional("list_tools")
        tools: list[dict[str, Any]] = []
        servers: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            maybe_tools = raw.get("tools", [])
            if isinstance(maybe_tools, list):
                tools = [item for item in maybe_tools if isinstance(item, dict)]
            maybe_servers = raw.get("mcp_servers", [])
            if isinstance(maybe_servers, list):
                servers = [
                    item if isinstance(item, dict) else {"name": str(item)}
                    for item in maybe_servers
                ]
        elif isinstance(raw, list):
            tools = [item for item in raw if isinstance(item, dict)]
        if not servers:
            servers = self._mcp_servers()
        send_json(h, {"tools": tools, "mcp_servers": servers})

    def _mcp_servers(self) -> list[dict[str, Any]]:
        raw = self.call_optional("list_mcp_servers")
        if raw is MISSING or raw is None:
            raw = self.call_optional("mcp_servers")
        if isinstance(raw, dict):
            raw = raw.get("mcp_servers", [])
        if isinstance(raw, list):
            return [item if isinstance(item, dict) else {"name": str(item)} for item in raw]
        return []

    def api_mcp_list(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        send_json(h, {"mcp_servers": self._mcp_servers()})

    def api_mcp_add(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        if not body.get("name"):
            raise ApiError(400, "INVALID_MCP_SERVER", "MCP Server 配置缺少 name 字段")
        result: Any = MISSING
        for name in ("add_mcp_server", "register_mcp_server", "save_mcp_server"):
            fn = getattr(self.platform, name, None)
            if callable(fn):
                result = self.call(name, body)
                break
        if result is MISSING:
            raise ApiError(501, "NOT_IMPLEMENTED", "平台门面暂不支持添加 MCP Server")
        server = result if isinstance(result, dict) else body
        send_json(h, {"ok": True, "server": server, "mcp_servers": self._mcp_servers()})

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    @staticmethod
    def _build_run_request(raw: dict[str, Any]) -> RunRequest:
        workflow_id = str(raw.get("workflow_id") or raw.get("workflowId") or "").strip()
        scenario = str(raw.get("scenario") or "").strip()
        if not workflow_id and scenario:
            workflow_id = scenario if scenario.startswith("wf-") else f"wf-{scenario}"
        if not workflow_id:
            raise ApiError(400, "INVALID_RUN", "缺少 workflow_id（或 scenario）字段")
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict):
            inputs = raw.get("input")
        if not isinstance(inputs, dict):
            inputs = {}
        team = raw.get("team")
        if not isinstance(team, list):
            team = []
        try:
            max_steps = int(raw.get("max_steps", 500))
        except (TypeError, ValueError):
            max_steps = 500
        auto_approve = raw.get("auto_approve", True)
        allowed = {field.name for field in dataclasses.fields(RunRequest)}
        kwargs: dict[str, Any] = {
            "workflow_id": workflow_id,
            "inputs": dict(inputs),
            "team": [str(item) for item in team],
            "policy": normalize_policy_or_400(str(raw.get("policy") or "capability")),
            "max_steps": max_steps,
            "label": str(raw.get("label") or ""),
            "scenario": scenario,
            "auto_approve": bool(auto_approve),
        }
        kwargs = {key: value for key, value in kwargs.items() if key in allowed}
        return RunRequest(**kwargs)

    def _start_run(self, raw: dict[str, Any]) -> dict[str, Any]:
        request = self._build_run_request(raw)
        try:
            result = self.call("start_run", request)
        except ApiError as exc:
            if exc.status == 500:
                if "并发" in exc.message:
                    raise ApiError(429, "TOO_MANY_RUNS", exc.message) from exc
                if "工作流不存在" in exc.message:
                    raise ApiError(
                        404, "WORKFLOW_NOT_FOUND", f"工作流不存在：{request.workflow_id}"
                    ) from exc
                if "未知调度策略" in exc.message:
                    raise ApiError(400, "INVALID_POLICY", exc.message) from exc
            raise
        run_id = ""
        status = "running"
        if isinstance(result, str):
            run_id = result
        elif isinstance(result, dict):
            run_id = str(result.get("run_id") or result.get("id") or "")
            status = str(result.get("status") or "running")
        elif result is not None:
            run_id = str(result)
        if not run_id:
            raise ApiError(500, "RUN_START_FAILED", "平台未返回有效的 run_id")
        log.info("启动运行 %s（工作流 %s，策略 %s）", run_id, request.workflow_id, request.policy)
        return {
            "run_id": run_id,
            "status": status,
            "workflow_id": request.workflow_id,
            "policy": request.policy,
        }

    def api_run_start(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        send_json(h, self._start_run(body))

    def api_demo_run(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        raw = dict(body)
        if not raw.get("workflow_id") and not raw.get("scenario"):
            raw["scenario"] = "ai4s-air-quality"
        payload = self._start_run(raw)
        payload["scenario"] = raw.get("scenario") or "ai4s-air-quality"
        send_json(h, payload)

    def api_scenarios(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        fn = getattr(self.platform, "list_scenarios", None)
        scenarios = []
        if callable(fn):
            try:
                scenarios = self.bridge.invoke(fn, timeout=10.0) or []
            except Exception as exc:  # pragma: no cover - 降级
                log.warning("读取场景目录失败：%s", exc)
        send_json(h, {"scenarios": scenarios, "count": len(scenarios)})

    def api_runs(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        limit = 50
        raw_limit = (query.get("limit") or [None])[0]
        if raw_limit:
            try:
                limit = max(1, min(200, int(raw_limit)))
            except ValueError:
                pass
        runs = self._list_runs(limit)
        send_json(h, {"runs": runs, "count": len(runs)})

    def api_run_detail(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        run = self._get_run(run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", f"运行不存在：{run_id}")
        send_json(h, run)

    def api_run_cancel(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        result = self.call("cancel_run", run_id)
        send_json(h, {"run_id": run_id, "cancelled": bool(result)})

    def api_run_verification(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        verification = self.call_optional("get_verification", run_id)
        loop = self.call_optional("get_loop_status", run_id)
        run = self._get_run(run_id) or {}
        if verification is MISSING or verification is None:
            verification = run.get("verification") or []
        if loop is MISSING or loop is None:
            loop = run.get("self_loop") or {}
        if not isinstance(verification, list):
            verification = [verification] if isinstance(verification, dict) else []
        confidence = None
        if verification:
            last = verification[-1]
            if isinstance(last, dict) and isinstance(last.get("confidence"), (int, float)):
                confidence = float(last["confidence"])
        if confidence is None:
            confidence = self._run_confidence(run)
        send_json(
            h,
            {
                "run_id": run_id,
                "verification": verification,
                "loop": loop if isinstance(loop, dict) else {"raw": loop},
                "self_loop": loop if isinstance(loop, dict) else {"raw": loop},
                "confidence": confidence,
                "layers": VERIFY_LAYERS,
                "checkers": VERIFY_CHECKERS,
            },
        )

    def api_run_loop(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        loop = self.call_optional("get_loop_status", run_id)
        if loop is MISSING or loop is None:
            run = self._get_run(run_id) or {}
            loop = run.get("self_loop") or {}
        send_json(h, {"run_id": run_id, "self_loop": loop})

    def api_run_report(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        report = self.call_optional("get_report", run_id)
        if report is MISSING or report is None:
            raise ApiError(404, "REPORT_NOT_FOUND", f"运行 {run_id} 暂无报告")
        send_json(h, {"run_id": run_id, "report": report})

    def api_run_artifacts(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        artifacts = self._run_artifacts(run_id)
        send_json(h, {"run_id": run_id, "artifacts": artifacts})

    def _run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        fn = getattr(self.platform, "list_artifacts", None)
        if callable(fn):
            try:
                raw = self.bridge.invoke(
                    fn, run_id, timeout=float(self.config.get("call_timeout_s") or 60.0)
                )
            except Exception as exc:
                log.warning("list_artifacts 调用失败：%s", exc)
                raw = None
            if isinstance(raw, dict):
                raw = raw.get("artifacts", [])
            if isinstance(raw, list):
                return [item if isinstance(item, dict) else {"name": str(item)} for item in raw]
        run = self._get_run(run_id) or {}
        for source in (
            run.get("artifacts"),
            (run.get("metrics") or {}).get("artifacts") if isinstance(run.get("metrics"), dict) else None,
            (run.get("variables") or {}).get("artifacts") if isinstance(run.get("variables"), dict) else None,
        ):
            if isinstance(source, list):
                return [item if isinstance(item, dict) else {"name": str(item)} for item in source]
            if isinstance(source, dict):
                return [{"name": str(key), **(value if isinstance(value, dict) else {})} for key, value in source.items()]
        return []

    def api_artifact_download(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        name = unquote(m.group("name"))
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ApiError(400, "BAD_ARTIFACT_NAME", "产物名称不合法")
        path = self._resolve_artifact_path(run_id, name)
        if path is None or not path.is_file():
            raise ApiError(404, "ARTIFACT_NOT_FOUND", f"产物不存在：{name}")
        send_file(h, path, download_name=name)

    def _resolve_artifact_path(self, run_id: str, name: str) -> Path | None:
        fn = getattr(self.platform, "artifact_path", None)
        if callable(fn):
            try:
                raw = self.bridge.invoke(fn, run_id, name)
            except Exception as exc:
                log.warning("artifact_path 调用失败：%s", exc)
                raw = None
            if raw:
                candidate = Path(str(raw)).resolve()
                return self._ensure_within_workspace(candidate)
        run = self._get_run(run_id) or {}
        metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
        directories: list[Path] = []
        for candidate_dir in (
            (metrics or {}).get("artifacts_dir"),
            run.get("artifacts_dir"),
        ):
            if candidate_dir:
                directories.append(Path(str(candidate_dir)))
        directories.extend(
            [
                self.workspace_dir / "data" / "runs" / run_id,
                self.workspace_dir / "data" / "runs" / run_id / "artifacts",
                self.workspace_dir / "runs" / run_id / "artifacts",
                self.workspace_dir / "artifacts" / run_id,
                self.workspace_dir / "runs" / run_id,
            ]
        )
        for directory in directories:
            try:
                candidate = (directory / name).resolve()
            except OSError:  # pragma: no cover - 防御
                continue
            safe = self._ensure_within_workspace(candidate)
            if safe is not None and safe.is_file():
                return safe
        return None

    def _ensure_within_workspace(self, path: Path) -> Path | None:
        try:
            path.relative_to(self.workspace_dir)
        except ValueError:
            return None
        return path

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def _last_event_id(self, h: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> int:
        raw = h.headers.get("Last-Event-ID") or (query.get("last_event_id") or [""])[0]
        if not raw:
            raw = (query.get("since_seq") or [""])[0]
        try:
            return max(0, int(str(raw).strip()))
        except (TypeError, ValueError):
            return 0

    def _open_subscription(self, run_id: str, last_id: int) -> Any:
        """建立事件订阅。

        平台门面的 ``subscribe`` 签名兼容两种实现：
        ``subscribe(run_id)``（回放由 ``get_events`` 负责）与
        ``subscribe(run_id, last_event_id)``。这里按真实签名决定传参个数。
        """
        subscribe = getattr(self.platform, "subscribe", None)
        if not callable(subscribe):
            return None
        args: list[Any] = [run_id]
        try:
            sig = inspect.signature(subscribe)
            positional = [
                p
                for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values())
            if len(positional) >= 2 or has_varargs:
                args.append(last_id)
        except (TypeError, ValueError):  # pragma: no cover - 内置/扩展函数
            args.append(last_id)
        return self.bridge.invoke(subscribe, *args)

    def api_run_events(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        run_id = unquote(m.group("run_id"))
        run = self._get_run(run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", f"运行不存在：{run_id}")
        last_id = self._last_event_id(h, query)
        heartbeat = max(0.2, float(self.config.get("sse_heartbeat_s") or 15.0))
        replay_limit = int(self.config.get("sse_replay_limit") or SSE_REPLAY_LIMIT)

        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache, no-transform")
        h.send_header("Connection", "close")
        h.send_header("X-Accel-Buffering", "no")
        for name, value in SECURITY_HEADERS.items():
            h.send_header(name, value)
        h.end_headers()
        h._headers_sent = True  # type: ignore[attr-defined]
        h.close_connection = True
        subscription: Any = None
        last_seq = last_id
        finished = False
        try:
            h.wfile.write(b"retry: 3000\n\n")
            h.wfile.flush()
            subscription = self._open_subscription(run_id, last_id)
            if subscription is not None:
                # 先回放历史（订阅已建立，回放与实时之间不会丢事件），再消费实时队列
                try:
                    history = self.call("get_events", run_id, last_id, replay_limit) or []
                except ApiError:
                    history = []
                for event in history:
                    data = self._send_sse_event(h, event, last_seq)
                    if data["seq"] > last_seq:
                        last_seq = data["seq"]
                        # 回放段要把 finished 累积起来：运行结束后紧跟的 log/artifact
                        # 事件不能把已置位的 finished 覆盖回 False，否则连接永不关闭。
                        finished = finished or data["finished"]
                while not finished:
                    event = self._subscription_get(subscription, heartbeat)
                    if event is None:
                        h.wfile.write(b": ping\n\n")
                        h.wfile.flush()
                        continue
                    data = self._send_sse_event(h, event, last_seq)
                    if data["seq"] > last_seq:
                        last_seq = data["seq"]
                    finished = finished or data["finished"]
            else:
                # 降级：门面没有 subscribe 时轮询 get_events
                while not finished:
                    events = self.call_optional("get_events", run_id, last_seq, 200)
                    if events is MISSING:
                        raise ApiError(501, "NOT_IMPLEMENTED", "平台门面未提供事件订阅能力")
                    sent_any = False
                    for event in events or []:
                        data = self._send_sse_event(h, event, last_seq)
                        if data["seq"] > last_seq:
                            last_seq = data["seq"]
                            finished = data["finished"]
                        sent_any = True
                    if not sent_any:
                        h.wfile.write(b": ping\n\n")
                        h.wfile.flush()
                        time.sleep(min(heartbeat, 1.0))
                    if finished:
                        break
                    if not self._run_active(run_id):
                        break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            log.debug("SSE 客户端断开：%s", run_id)
        finally:
            if subscription is not None:
                close = getattr(subscription, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover - 防御
                        log.debug("关闭订阅失败", exc_info=True)

    def _subscription_get(self, subscription: Any, timeout: float) -> Any:
        get = getattr(subscription, "get", None)
        if not callable(get):
            return None
        try:
            return self.bridge.invoke(get, timeout)
        except Exception as exc:
            log.debug("订阅读取失败：%s", exc)
            return None

    def _run_active(self, run_id: str) -> bool:
        try:
            run = self._get_run(run_id)
        except ApiError:
            return False
        if run is None:
            return False
        return str(run.get("status")) in ("pending", "running", "paused")

    def _send_sse_event(
        self, h: BaseHTTPRequestHandler, event: Any, last_seq: int
    ) -> dict[str, Any]:
        data = trim_event(event)
        seq = _event_seq(data)
        if seq and seq <= last_seq:
            return {"seq": last_seq, "finished": False}
        etype = re.sub(r"[^A-Za-z0-9_.-]", "", str(data.get("type") or "message"))
        frame = "id: {}\nevent: {}\ndata: {}\n\n".format(
            seq, etype, json.dumps(data, ensure_ascii=False, default=str)
        ).encode("utf-8")
        h.wfile.write(frame)
        h.wfile.flush()
        finished = etype in ("run.finished", "run.cancelled", "run.rejected")
        return {"seq": max(seq, last_seq), "finished": finished}

    # ------------------------------------------------------------------
    # 验证 / 模型设置 / 统计
    # ------------------------------------------------------------------
    def api_verify_strategies(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        raw = self.call_optional("verify_strategies")
        if isinstance(raw, dict):
            send_json(h, raw)
            return
        if isinstance(raw, list):
            send_json(h, {"checkers": raw, "layers": VERIFY_LAYERS})
            return
        send_json(
            h,
            {
                "checkers": VERIFY_CHECKERS,
                "layers": VERIFY_LAYERS,
                "pipeline": [
                    "结构校验",
                    "数值比对",
                    "独立复算",
                    "交叉复核",
                    "统计阈值",
                ],
            },
        )

    def _llm_status_raw(self) -> dict[str, Any]:
        fn = getattr(self.platform, "llm_status", None)
        if not callable(fn):
            return {}
        try:
            raw = self.bridge.invoke(fn, timeout=10.0)
        except Exception as exc:  # pragma: no cover - 降级
            return {"error": str(exc)}
        return raw if isinstance(raw, dict) else {"status": raw}

    def api_llm_config(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        status = _mask_config(self._llm_status_raw())
        send_json(h, {"llm": status, "config": status})

    def api_llm_config_save(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: dict[str, Any], query: Any
    ) -> None:
        provider = str(body.get("provider") or "").strip()
        if provider and provider not in ("mock", "openai_compat", "openai", "auto"):
            raise ApiError(400, "INVALID_PROVIDER", f"不支持的 Provider：{provider}")
        payload: dict[str, Any] = {}
        for key in ("provider", "base_url", "model", "api_key", "timeout_s", "test"):
            if key in body and body[key] not in (None, ""):
                payload[key] = body[key]
        result = self.call("set_llm_config", payload)
        status = result if isinstance(result, dict) else self._llm_status_raw()
        if isinstance(status, dict) and "llm" in status and isinstance(status["llm"], dict):
            status = status["llm"]
        send_json(h, {"ok": True, "llm": _mask_config(status), "config": _mask_config(status)})

    def api_stats_timeseries(
        self, h: BaseHTTPRequestHandler, m: re.Match[str], body: Any, query: Any
    ) -> None:
        raw = self.call_optional("stats_timeseries")
        if isinstance(raw, (list, dict)) and raw is not MISSING:
            send_json(h, {"series": raw})
            return
        runs = self._list_runs(100)
        points = []
        for run in sorted(runs, key=lambda item: item.get("created_at") or 0):
            points.append(
                {
                    "ts": run.get("created_at") or run.get("started_at") or 0,
                    "run_id": run.get("run_id"),
                    "workflow_id": run.get("workflow_id"),
                    "status": run.get("status"),
                    "success": run.get("status") == "succeeded",
                    "duration_s": self._run_duration(run),
                    "tokens": self._run_tokens(run),
                    "confidence": self._run_confidence(run),
                }
            )
        send_json(h, {"points": points, "series": points, "count": len(points)})


# ---------------------------------------------------------------------------
# HTTP 处理与服务器
# ---------------------------------------------------------------------------
def _build_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class AgentWeaveHandler(BaseHTTPRequestHandler):
        """单连接处理：GET/POST 统一分发；异常转为统一 JSON 错误体。"""

        protocol_version = "HTTP/1.1"
        server_version = "AgentWeave/1.0"
        sys_version = ""
        _headers_sent = False

        def do_GET(self) -> None:  # noqa: N802 - 标准库约定
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._headers_sent = False
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.send_header("Allow", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(b"{}")

        def _handle(self, method: str) -> None:
            try:
                app.dispatch(self, method, self.path)
            except ApiError as exc:
                self._send_error_json(exc.status, exc.code, exc.message)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.debug("客户端连接中断：%s", self.path)
            except Exception as exc:  # pragma: no cover - 兜底
                log.exception("处理请求失败：%s %s", method, self.path)
                self._send_error_json(500, "INTERNAL", f"服务内部错误：{exc}")

        def _send_error_json(self, status: int, code: str, message: str) -> None:
            if getattr(self, "_headers_sent", False):
                self.close_connection = True
                return
            try:
                send_json(self, {"error": {"code": code, "message": message}}, status)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

    return AgentWeaveHandler


class AgentWeaveHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器：SSE 长连接与普通 REST 互不阻塞。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, server_address: tuple[str, int], app: WebApp) -> None:
        self.app = app
        handler = _build_handler(app)
        super().__init__(server_address, handler)


def create_app(platform: Any, config: dict[str, Any] | None = None) -> AgentWeaveHTTPServer:
    """组装 Web 应用并返回已绑定端口的 ``ThreadingHTTPServer``。

    :param platform: 平台门面（``agentweave.runtime.platform.Platform`` 或 FakePlatform）
    :param config: 覆盖默认配置（host/port/web_dir/sse_heartbeat_s 等）
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})
    app = WebApp(platform, cfg)
    server = AgentWeaveHTTPServer((str(cfg["host"]), int(cfg["port"])), app)
    server.config = cfg  # type: ignore[attr-defined]
    return server
