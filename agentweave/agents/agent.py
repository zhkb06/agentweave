"""Agent 运行时：ReAct（推理 + 工具调用）循环。

一次 :meth:`AgentRuntime.run` 的流程
-----------------------------------
1. 组装 system prompt（角色设定 + 元信息 + 输出协议）与 user prompt
   （节点任务 + 上游上下文 + 上一轮验证反馈 + 历史记忆）；
2. 通过 :class:`~agentweave.llm.router.ModelRouter` 调用模型（失败由路由器回退 Mock）；
3. 解析模型输出中的工具调用 JSON（``{"tool": "...", "args": {...}}``，
   或 OpenAI 原生 ``tool_calls``），执行工具并把观察结果回注对话；
4. 当模型不再请求工具时，把最终 JSON 解析为 ``AgentResult.output``；
5. 全程发出 ``agent.thought`` / ``tool.called`` / ``tool.result`` / ``agent.decision`` 事件，
   并汇总 token、延迟、迭代次数与工具调用记录。

输出契约（与 Lead 对齐）：``AgentResult.output`` 为 dict，至少含中文 ``summary``，
可选 ``evidence`` / ``confidence`` 及各角色专属字段（tasks、metrics、feedback 等）。
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

from ..core.types import AgentResult, AgentSpec, EventType, ToolCallRecord, new_id

_LOGGER = logging.getLogger(__name__)

#: 回注给模型的单次工具观察最大字符数（避免上下文爆炸）。
MAX_OBSERVATION_CHARS = 6000
#: 上游上下文 JSON 的最大字符数。
MAX_CONTEXT_CHARS = 4000
#: 默认最大迭代轮数（spec.max_iterations 缺省时）。
DEFAULT_MAX_ITERATIONS = 8


# ----------------------------------------------------------------------
# JSON 解析辅助
# ----------------------------------------------------------------------
def _strip_code_fences(text: str) -> str:
    """去掉 ```json ... ``` 围栏，只留内部文本。"""
    raw = str(text or "").strip()
    if "```" not in raw:
        return raw
    parts = raw.split("```")
    for chunk in parts[1:]:
        body = chunk.strip()
        if body.lower().startswith("json"):
            body = body[4:].strip()
        if body.startswith("{") or body.startswith("["):
            return body
    return raw


def extract_json(text: str) -> Any | None:
    """从模型输出中尽量提取第一个合法 JSON 对象/数组（容忍前后解释文字）。"""
    raw = _strip_code_fences(text)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(raw[index:])
            return value
        except (ValueError, TypeError):
            continue
    return None


def _parse_arguments(value: Any) -> dict[str, Any]:
    """把工具参数统一成 dict（字符串按 JSON 解析，失败则包一层 ``raw``）。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = extract_json(value)
        if isinstance(parsed, dict):
            return parsed
        return {"raw": value}
    return {}


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[已截断 {len(text) - limit} 字符]"


def _first_line(text: str, limit: int = 200) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _clamp_confidence(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


class AgentRuntime:
    """执行单个 Agent 的 ReAct 循环。

    参数
    ----
    spec:
        Agent 定义（来自 ``config/agents.json``）。
    llm:
        :class:`ModelRouter`（或任意带 ``async complete()`` 的提供方）。
    tools:
        :class:`ToolRegistry`，可为 None（无工具模式）。
    bus:
        事件总线，可为 None。
    memory:
        :class:`AgentMemory`，可为 None。
    run_id:
        预置的 run 标识（``run()`` 传入的同名参数优先）。
    """

    def __init__(
        self,
        spec: AgentSpec,
        llm: Any = None,
        tools: Any = None,
        bus: Any = None,
        memory: Any = None,
        run_id: str = "",
        max_iterations: int | None = None,
    ) -> None:
        if not isinstance(spec, AgentSpec):
            spec = AgentSpec.from_dict(dict(spec))
        self.spec = spec
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.memory = memory
        self.run_id = str(run_id or "")
        self.max_iterations = int(max_iterations or spec.max_iterations or DEFAULT_MAX_ITERATIONS)
        self._logger = logging.getLogger(f"{__name__}.{spec.id}")

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        run_id: str = "",
        node_id: str = "",
        *,
        context: dict[str, Any] | None = None,
        feedback: str = "",
        max_iterations: int | None = None,
    ) -> AgentResult:
        """执行一次 Agent 任务，返回 :class:`AgentResult`。"""
        started = time.perf_counter()
        effective_run = str(run_id or self.run_id or "")
        node_id = str(node_id or "")
        max_iter = max(1, int(max_iterations or self.max_iterations))
        messages = self._build_messages(prompt, context or {}, feedback)

        result = AgentResult(output=None, confidence=0.0, model="")
        tool_schemas = self._tool_schemas()
        observations = 0

        for iteration in range(1, max_iter + 1):
            try:
                llm_result = await self._call_llm(messages, tool_schemas, effective_run, node_id)
            except Exception as exc:  # noqa: BLE001 - 单次 LLM 失败不应让工作流崩溃
                result.error = f"LLM 调用失败: {type(exc).__name__}: {exc}"
                self._logger.warning("%s", result.error)
                break

            result.model = str(getattr(llm_result, "model", "") or result.model)
            result.tokens_in += int(getattr(llm_result, "tokens_in", 0) or 0)
            result.tokens_out += int(getattr(llm_result, "tokens_out", 0) or 0)
            content = str(getattr(llm_result, "content", "") or "")
            parsed = extract_json(content)

            thought = ""
            if isinstance(parsed, dict):
                thought = str(parsed.get("thought") or parsed.get("reasoning") or "").strip()
            if thought:
                result.thoughts.append(thought)
                self._emit(
                    EventType.AGENT_THOUGHT,
                    effective_run,
                    node_id,
                    {"text": _truncate(thought, 500), "step": iteration},
                )

            tool_call = self._extract_tool_call(llm_result, parsed)
            if tool_call is None:
                # ---- 最终答案 ----
                if not content.strip():
                    result.error = "模型返回空内容"
                    result.confidence = 0.3
                else:
                    output = self._normalize_output(parsed, content)
                    result.output = output
                    result.confidence = _clamp_confidence(output.get("confidence"), 0.8)
                    result.iterations = iteration
                    summary = str(output.get("summary") or "").strip()
                    if not summary:
                        summary = _first_line(json.dumps(output, ensure_ascii=False), 200)
                    self._emit(
                        EventType.AGENT_DECISION,
                        effective_run,
                        node_id,
                        {"text": _truncate(summary, 600), "confidence": round(result.confidence, 3)},
                    )
                    self._remember(node_id, summary, output)
                result.iterations = iteration
                result.latency_s = time.perf_counter() - started
                return result

            # ---- 工具调用 ----
            name, args, call_id = tool_call
            observations += 1
            if not self._registry_emits:
                self._emit(
                    EventType.TOOL_CALLED,
                    effective_run,
                    node_id,
                    {"name": name, "args": _truncate(json.dumps(args, ensure_ascii=False, default=str), 800)},
                )
            ok, value, error, duration = await self._execute_tool(name, args, effective_run, node_id)
            record = ToolCallRecord(
                call_id=call_id,
                name=name,
                args=args,
                result=value if ok else None,
                error=error or None,
                duration_s=duration,
            )
            result.tool_calls.append(record)
            observation = self._observation_text(ok, value, error)
            if not self._registry_emits:
                self._emit(
                    EventType.TOOL_RESULT,
                    effective_run,
                    node_id,
                    {
                        "name": name,
                        "ok": ok,
                        "duration_s": round(duration, 4),
                        "preview": _truncate(observation, 400),
                        "error": error or "",
                    },
                )

            # 把「模型请求 → 工具观察」写回对话（同时兼容 Mock 的 role=tool 判定）
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False, default=str)},
                        }
                    ],
                }
            )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": observation})

        # 迭代耗尽 / LLM 失败
        result.iterations = max_iter
        result.latency_s = time.perf_counter() - started
        if result.error is None:
            result.error = f"达到最大迭代次数（{max_iter}），仍未得到最终结论"
        if result.confidence <= 0:
            result.confidence = 0.3
        self._logger.warning("Agent %s 未收敛：%s（工具调用 %d 次）", self.spec.id, result.error, observations)
        return result

    # ------------------------------------------------------------------
    # Prompt 构造
    # ------------------------------------------------------------------
    def _tool_names(self) -> list[str]:
        return [str(t) for t in (self.spec.tools or []) if str(t).strip()]

    def _build_messages(
        self,
        prompt: str,
        context: dict[str, Any],
        feedback: str,
    ) -> list[dict[str, str]]:
        """构造 system + user 消息。"""
        tool_names = self._tool_names()
        meta = (
            f"[元信息] agent_id={self.spec.id}; role={self.spec.role}; name={self.spec.name}; "
            f"可用工具={','.join(tool_names) if tool_names else '无'}"
        )
        protocol = (
            "[输出协议] 你是 ReAct 智能体，只能输出 JSON，不要输出解释文字：\n"
            '1) 需要工具时输出：{"thought": "为什么调用", "tool": "<工具名>", "args": {<参数>}}；\n'
            '2) 信息足够时输出最终结果：{"summary": "中文一句话结论", "evidence": ["证据"], '
            '"confidence": 0.0~1.0, ...角色专属字段}。\n'
            "工具返回的观察会以 role=tool 消息追加给你，看到观察后请继续推理或给出最终 JSON。"
        )
        system = "\n\n".join(part for part in [self.spec.system_prompt.strip(), meta, protocol] if part)

        sections = [f"【任务】\n{prompt.strip()}"]
        if context:
            try:
                context_text = json.dumps(context, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                context_text = str(context)
            sections.append(f"【上游上下文】\n{_truncate(context_text, MAX_CONTEXT_CHARS)}")
        memory_text = self._recall_memory(prompt)
        if memory_text:
            sections.append(f"【历史记忆】\n{memory_text}")
        if str(feedback or "").strip():
            sections.append(f"【上一轮验证反馈】\n{str(feedback).strip()}")
        user = "\n\n".join(sections)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _recall_memory(self, prompt: str) -> str:
        if self.memory is None:
            return ""
        try:
            hits = self.memory.recall(prompt, k=3)
        except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
            return ""
        lines: list[str] = []
        for item in hits or []:
            text = item.get("text") if isinstance(item, dict) else item
            text = str(text or "").strip()
            if text:
                lines.append(f"- {_truncate(text, 240)}")
        return "\n".join(lines)

    def _remember(self, node_id: str, summary: str, output: dict[str, Any]) -> None:
        if self.memory is None or not summary:
            return
        try:
            self.memory.note(
                f"[{node_id}] {summary}",
                node_id=node_id,
                agent_id=self.spec.id,
                kind="agent_result",
                meta={"keys": sorted(str(k) for k in output)},
            )
        except Exception:  # noqa: BLE001
            self._logger.debug("写入 AgentMemory 失败", exc_info=True)

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------
    def _tool_schemas(self) -> list[dict[str, Any]] | None:
        if self.tools is None:
            return None
        names = self._tool_names()
        if not names:
            return None
        try:
            if hasattr(self.tools, "openai_tools"):
                schemas = list(self.tools.openai_tools(names) or [])
                return schemas or None
            specs = []
            for name in names:
                spec = self.tools.get(name) if hasattr(self.tools, "get") else None
                if spec is not None:
                    specs.append(
                        {
                            "type": "function",
                            "function": {
                                "name": spec.name,
                                "description": spec.description,
                                "parameters": spec.parameters or {"type": "object", "properties": {}},
                            },
                        }
                    )
            return specs or None
        except Exception:  # noqa: BLE001 - 工具描述失败时退化为无工具
            self._logger.debug("构造工具 schema 失败", exc_info=True)
            return None

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        run_id: str,
        node_id: str,
    ) -> Any:
        if self.llm is None:
            raise RuntimeError("未配置 LLM（AgentRuntime.llm 为 None）")
        kwargs: dict[str, Any] = {
            "tools": tools,
            "temperature": float(self.spec.temperature if self.spec.temperature is not None else 0.2),
            "max_tokens": 2048,
        }
        # 路由器支持角色路由与事件上下文；直接提供方只接受基础参数
        try:
            params = inspect.signature(self.llm.complete).parameters
        except (TypeError, ValueError):  # pragma: no cover - 动态对象
            params = {}
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        if "role" in params or accepts_kwargs:
            kwargs["role"] = self.spec.role
        if "run_id" in params or accepts_kwargs:
            kwargs.update({"run_id": run_id, "node_id": node_id or None, "agent_id": self.spec.id})
        complete = self.llm.complete
        return await complete(messages, **kwargs)

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------
    @property
    def _registry_emits(self) -> bool:
        """工具注册表自带事件总线时，避免 tool.* 事件重复发送。"""
        return bool(getattr(self.tools, "bus", None))

    @staticmethod
    def _extract_tool_call(llm_result: Any, parsed: Any) -> tuple[str, dict[str, Any], str] | None:
        """从 LLM 结果中提取 ``(工具名, 参数, call_id)``；无工具调用返回 None。"""
        raw_calls = getattr(llm_result, "tool_calls", None) or []
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(item.get("name") or item.get("tool") or function.get("name") or "").strip()
            if not name:
                continue
            args = _parse_arguments(
                item.get("args")
                if item.get("args") is not None
                else item.get("arguments")
                if item.get("arguments") is not None
                else function.get("arguments")
            )
            return name, args, str(item.get("id") or new_id("call"))
        if isinstance(parsed, dict):
            name = str(parsed.get("tool") or parsed.get("tool_name") or "").strip()
            if name:
                args = _parse_arguments(parsed.get("args") if parsed.get("args") is not None else parsed.get("arguments"))
                return name, args, str(parsed.get("call_id") or new_id("call"))
        return None

    async def _execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        run_id: str,
        node_id: str,
    ) -> tuple[bool, Any, str, float]:
        """执行工具，返回 ``(ok, value, error, duration_s)``；错误回注而不抛出。"""
        if self.tools is None:
            return False, None, "工具层不可用（ToolRegistry 未注入）", 0.0
        allowed = self._tool_names()
        if name not in allowed:
            # spec.tools 是白名单：为空表示该 Agent 不授权任何工具（需显式配置）
            return False, None, f"工具 {name!r} 未授权：不在 Agent {self.spec.id} 的授权列表 {allowed} 中", 0.0
        started = time.perf_counter()
        try:
            try:
                outcome = await self.tools.call(
                    name,
                    args,
                    run_id=run_id,
                    node_id=node_id or None,
                    agent_id=self.spec.id,
                )
            except TypeError:
                outcome = await self.tools.call(name, args)
        except Exception as exc:  # noqa: BLE001 - 工具异常统一回注
            return False, None, f"{type(exc).__name__}: {exc}", time.perf_counter() - started
        duration = float(getattr(outcome, "duration_s", 0.0) or 0.0) or (time.perf_counter() - started)
        ok = bool(getattr(outcome, "ok", False))
        value = getattr(outcome, "value", None)
        error = str(getattr(outcome, "error", "") or "")
        return ok, value, error, duration

    @staticmethod
    def _observation_text(ok: bool, value: Any, error: str) -> str:
        """把工具结果序列化为回注对话的观察文本。"""
        if not ok:
            return json.dumps({"ok": False, "error": error or "工具执行失败"}, ensure_ascii=False)
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(value)
        return _truncate(payload, MAX_OBSERVATION_CHARS)

    # ------------------------------------------------------------------
    # 输出归一化 / 事件
    # ------------------------------------------------------------------
    def _normalize_output(self, parsed: Any, content: str) -> dict[str, Any]:
        """把模型输出归一化为契约 dict（summary/evidence/confidence + 角色字段）。"""
        if isinstance(parsed, dict):
            output = dict(parsed)
            # 兼容旧式 {"content": {...}} 包裹：拍平到顶层
            inner = output.get("content")
            if isinstance(inner, dict) and not (set(output) - {"summary", "content", "evidence", "confidence"}):
                merged = dict(inner)
                merged.update({k: v for k, v in output.items() if k != "content"})
                output = merged
            summary = output.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                text = output.get("text") or output.get("conclusion") or ""
                output["summary"] = _first_line(str(text), 300) or f"{self.spec.name} 已完成节点任务"
            evidence = output.get("evidence")
            if evidence is not None and not isinstance(evidence, list):
                output["evidence"] = [str(evidence)]
            output["confidence"] = _clamp_confidence(output.get("confidence"), 0.8)
            return output
        text = str(content or "").strip()
        return {
            "summary": _first_line(text, 300) or f"{self.spec.name} 已完成节点任务",
            "text": _truncate(text, MAX_CONTEXT_CHARS),
            "evidence": [],
            "confidence": 0.5,
        }

    def _emit(self, type_: str, run_id: str, node_id: str, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                type_,
                run_id=run_id,
                node_id=node_id or None,
                agent_id=self.spec.id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 - 事件失败不影响执行
            self._logger.debug("发送事件失败: %s", type_, exc_info=True)


__all__ = ["AgentRuntime", "extract_json", "MAX_OBSERVATION_CHARS"]
