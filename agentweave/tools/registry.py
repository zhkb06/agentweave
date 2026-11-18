"""工具注册表：统一注册、JSON Schema 宽松校验、超时/异常兜底与事件发布。

契约见 docs/CONTRACTS.md §3.2：

* :class:`ToolResult` 是唯一返回类型（``ok / value / error / duration_s / meta``）；
* :class:`ToolRegistry` 负责注册内置工具与 MCP 工具，按名调用、校验入参、控制超时；
* 每次调用发出 ``tool.called`` / ``tool.result`` 事件（payload 含参数摘要与结果预览，
  不包含完整大文本，避免事件总线被报告正文淹没）；
* ``risk_level == "high"`` 的工具在 ``require_approval`` 模式下会被拒绝，
  由上层（人工闸门）审批后放开。
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.types import EventType, ToolSpec

_LOGGER = logging.getLogger(__name__)

#: 工具结果预览长度上限（事件 payload 用）。
_PREVIEW_LIMIT = 400


@dataclass
class ToolResult:
    """工具调用的统一结果。"""

    ok: bool = False
    value: Any = None
    error: str = ""
    duration_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, value: Any = None, duration_s: float = 0.0, **meta: Any) -> "ToolResult":
        """构造成功结果。"""
        return cls(ok=True, value=value, duration_s=duration_s, meta=dict(meta))

    @classmethod
    def failure(cls, error: str, duration_s: float = 0.0, **meta: Any) -> "ToolResult":
        """构造失败结果。"""
        return cls(ok=False, value=None, error=str(error), duration_s=duration_s, meta=dict(meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_s": round(self.duration_s, 4),
            "meta": dict(self.meta),
        }

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class _ToolEntry:
    spec: ToolSpec
    fn: Callable[..., Any]
    accepts_ctx: bool = False
    calls: int = 0
    failures: int = 0
    total_duration_s: float = 0.0


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    """把任意结果压成短预览字符串。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = text.replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_args(schema: dict[str, Any] | None, args: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """按 JSON Schema 做宽松校验，返回 ``(补齐默认值后的参数, 错误列表)``。

    * 只检查 ``required`` / ``type`` / ``enum``；
    * 允许把数字字符串转成 number/integer，允许单个值被包装成数组；
    * 未声明的额外参数保留（宽松模式，方便 MCP 工具透传）。
    """
    schema = schema or {}
    provided = dict(args or {})
    errors: list[str] = []
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])

    for name in required:
        if name not in provided:
            errors.append(f"缺少必填参数: {name}")

    for name, raw in list(provided.items()):
        prop = properties.get(name) or {}
        expected = str(prop.get("type") or "").lower()
        if not expected:
            continue
        value = raw
        if expected in ("number", "integer") and isinstance(value, str):
            try:
                value = int(value) if expected == "integer" else float(value)
            except ValueError:
                errors.append(f"参数 {name} 需要 {expected}，收到字符串 {raw!r}")
                continue
        elif expected == "array" and not isinstance(value, (list, tuple)):
            value = [value]
        elif expected == "string" and isinstance(value, (int, float, bool)):
            value = str(value)
        elif expected == "boolean" and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        if expected and not isinstance(value, _TYPE_CHECKS.get(expected, (object,))):
            errors.append(f"参数 {name} 类型应为 {expected}")
            continue
        enum = prop.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            errors.append(f"参数 {name} 必须取 {enum} 之一")
            continue
        provided[name] = value

    for name, prop in properties.items():
        if name not in provided and "default" in prop:
            provided[name] = prop["default"]
    return provided, errors


class _NameList(list):
    """已注册工具名列表；同时支持 ``registry.names`` 与 ``registry.names()`` 两种写法。"""

    def __call__(self) -> list[str]:
        return list(self)


class ToolRegistry:
    """工具注册表（契约见 docs/CONTRACTS.md §3.2）。"""

    def __init__(
        self,
        workspace_dir: str = "",
        bus: Any = None,
        require_approval: bool = False,
    ) -> None:
        self.workspace_dir = str(workspace_dir or "")
        self.bus = bus
        self.require_approval = bool(require_approval)
        self._lock = threading.RLock()
        self._tools: dict[str, _ToolEntry] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------
    # 注册 / 查询
    # ------------------------------------------------------------------
    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> ToolSpec:
        """注册（或覆盖）一个工具实现。"""
        if not callable(fn):
            raise TypeError(f"工具 {getattr(spec, 'name', '?')} 的实现不可调用")
        try:
            params = inspect.signature(fn).parameters
            accepts_ctx = "ctx" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - 内建函数签名不可取时兜底
            accepts_ctx = False
        with self._lock:
            if spec.name not in self._tools:
                self._order.append(spec.name)
            self._tools[spec.name] = _ToolEntry(spec=spec, fn=fn, accepts_ctx=accepts_ctx)
        return spec

    def unregister(self, name: str) -> bool:
        """移除工具，返回是否存在。"""
        with self._lock:
            existed = self._tools.pop(name, None) is not None
            if existed and name in self._order:
                self._order.remove(name)
            return existed

    def get(self, name: str) -> ToolSpec | None:
        """按名取工具元数据。"""
        with self._lock:
            entry = self._tools.get(name)
            return entry.spec if entry else None

    def get_callable(self, name: str) -> Callable[..., Any] | None:
        """按名取工具实现（MCP 管理面板/测试用）。"""
        with self._lock:
            entry = self._tools.get(name)
            return entry.fn if entry else None

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        """返回工具元数据；给定 ``names`` 时按该顺序过滤。"""
        with self._lock:
            if names is None:
                return [self._tools[n].spec for n in self._order if n in self._tools]
            return [self._tools[n].spec for n in names if n in self._tools]

    @property
    def names(self) -> _NameList:
        """返回已注册工具名（按注册顺序，属性与调用两种写法均可用）。"""
        with self._lock:
            return _NameList(n for n in self._order if n in self._tools)

    def openai_tools(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """转换成 OpenAI Function Calling 的 tools 格式。"""
        tools: list[dict[str, Any]] = []
        for spec in self.specs(names):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters or {"type": "object", "properties": {}},
                    },
                }
            )
        return tools

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        ctx: Any = None,
        **event_kw: Any,
    ) -> ToolResult:
        """调用工具：校验 → 审批 → 执行（带超时） → 兜底异常 → 发事件。"""
        with self._lock:
            entry = self._tools.get(name)
        if entry is None:
            return ToolResult.failure(f"未知工具: {name}")

        started = time.perf_counter()
        validated, errors = validate_args(entry.spec.parameters, args)
        if errors:
            result = ToolResult.failure("；".join(errors), 0.0)
            self._after_call(entry, result, validated, ctx, event_kw)
            return result

        if self.require_approval and entry.spec.risk_level == "high":
            auto = bool(getattr(ctx, "auto_approve", False)) if ctx is not None else False
            if not auto:
                result = ToolResult.failure(
                    f"高风险工具 {name} 需要人工审批（require_approval=True）",
                    meta={"risk_level": entry.spec.risk_level},
                )
                self._after_call(entry, result, validated, ctx, event_kw)
                return result

        self._emit(
            EventType.TOOL_CALLED,
            {"name": name, "args": self._sanitize_args(validated), "risk_level": entry.spec.risk_level},
            ctx,
            event_kw,
            node_id=event_kw.get("node_id"),
            agent_id=event_kw.get("agent_id"),
        )

        timeout = float(entry.spec.timeout_s or 10.0)
        try:
            if entry.accepts_ctx:
                call_kwargs = dict(validated, ctx=ctx)
            else:
                call_kwargs = dict(validated)
            if inspect.iscoroutinefunction(entry.fn):
                value = await asyncio.wait_for(entry.fn(**call_kwargs), timeout=timeout)
            else:
                loop = asyncio.get_running_loop()
                value = await asyncio.wait_for(
                    loop.run_in_executor(None, functools.partial(entry.fn, **call_kwargs)),
                    timeout=timeout,
                )
            duration = time.perf_counter() - started
            result = ToolResult.success(value, duration)
        except asyncio.TimeoutError:
            duration = time.perf_counter() - started
            result = ToolResult.failure(f"工具 {name} 执行超时（>{timeout:g}s）", duration, timed_out=True)
        except Exception as exc:  # noqa: BLE001 - 工具异常统一转换为失败结果
            duration = time.perf_counter() - started
            _LOGGER.warning("工具 %s 执行失败：%s", name, exc, exc_info=_LOGGER.isEnabledFor(logging.DEBUG))
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", duration, tool=name)

        self._after_call(entry, result, validated, ctx, event_kw)
        return result

    # ------------------------------------------------------------------
    # 统计 / 内部
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, dict[str, Any]]:
        """返回各工具调用次数/失败次数/平均耗时。"""
        with self._lock:
            return {
                name: {
                    "calls": entry.calls,
                    "failures": entry.failures,
                    "avg_duration_s": round(entry.total_duration_s / entry.calls, 4) if entry.calls else 0.0,
                }
                for name, entry in self._tools.items()
            }

    def set_bus(self, bus: Any) -> None:
        """绑定/更新事件总线。"""
        self.bus = bus

    def bind_bus(self, bus: Any) -> None:
        self.set_bus(bus)

    def _after_call(
        self,
        entry: _ToolEntry,
        result: ToolResult,
        args: dict[str, Any],
        ctx: Any,
        event_kw: dict[str, Any],
    ) -> None:
        with self._lock:
            entry.calls += 1
            if not result.ok:
                entry.failures += 1
            entry.total_duration_s += float(result.duration_s or 0.0)
        self._emit(
            EventType.TOOL_RESULT,
            {
                "name": entry.spec.name,
                "ok": result.ok,
                "duration_s": round(result.duration_s, 4),
                "preview": _preview(result.error if not result.ok else result.value),
                "error": result.error,
            },
            ctx,
            event_kw,
            node_id=event_kw.get("node_id"),
            agent_id=event_kw.get("agent_id"),
            level="info" if result.ok else "warning",
        )

    @staticmethod
    def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
        """事件里的参数摘要：超长字符串截断，避免刷屏。"""
        clean: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str) and len(value) > 200:
                clean[key] = value[:200] + "…"
            else:
                clean[key] = value
        return clean

    def _emit(
        self,
        type_: str,
        payload: dict[str, Any],
        ctx: Any,
        event_kw: dict[str, Any],
        *,
        node_id: Any = None,
        agent_id: Any = None,
        level: str = "info",
    ) -> None:
        bus = getattr(ctx, "bus", None) if ctx is not None else None
        bus = bus or self.bus
        if bus is None:
            return
        run_id = event_kw.get("run_id") or (getattr(ctx, "run_id", "") if ctx is not None else "")
        try:
            bus.emit(
                type_,
                run_id=run_id or "",
                node_id=node_id,
                agent_id=agent_id,
                payload=payload,
                level=level,
            )
        except Exception:  # pragma: no cover - 事件异常不影响工具调用
            _LOGGER.debug("发送工具事件失败", exc_info=True)


__all__ = ["ToolResult", "ToolRegistry", "validate_args"]
