"""核心执行层测试辅助工具。

用可编程的 :class:`FakeCtx` 替代真实 RuntimeContext，把 LLM / 工具 / 人工闸门
变成确定性桩函数，从而在离线、毫秒级内测试拓扑推进、并行、条件、循环、
重试与验证逻辑。真实引擎 ``WorkflowEngine`` 全量参与执行，只替换外部依赖。

设计要点：
1. 每个调用都记录在 ``agent_calls`` / ``tool_calls`` / ``human_calls`` 中，
   便于断言「重跑了几次」「反馈是否注入」「工具参数是否正确」；
2. ``events`` 保存同步 emit 的全部事件（类型 + payload），用于断言事件契约；
3. Agent / 工具回调支持返回协程，方便构造真并发场景。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from agentweave.core.types import (
    AgentResult,
    VerifyCheckResult,
    VerifyResult,
)
from agentweave.runtime.state import RunState
from agentweave.tools.registry import ToolResult
from agentweave.workflow.engine import WorkflowEngine

__all__ = [
    "FakeAgents",
    "FakeCtx",
    "FakeVerifier",
    "agent_result",
    "events_of",
    "execute_workflow",
    "make_run",
]


class FakeAgents:
    """最小 Agent 注册表：``match`` 按技能重叠度打分（0.6 起步）。"""

    def __init__(self, specs: Iterable[Any] | None = None) -> None:
        self.specs: list[Any] = list(specs or [])
        if not self.specs:
            for agent_id, role, skills in (
                ("planner", "planner", ["planning"]),
                ("researcher", "researcher", ["research"]),
                ("data_engineer", "data_engineer", ["data"]),
                ("statistician", "statistician", ["statistics", "python"]),
                ("verifier", "verifier", ["verify"]),
                ("critic", "critic", ["review"]),
                ("writer", "writer", ["writing"]),
            ):
                self.specs.append(
                    SimpleNamespace(id=agent_id, role=role, skills=skills, tools=[])
                )

    def get(self, agent_id: str) -> Any | None:
        """按 id 取 AgentSpec（找不到返回 None）。"""
        return next((spec for spec in self.specs if spec.id == agent_id), None)

    def match(
        self, skills: list[str] | None = None, tools: list[str] | None = None
    ) -> list[tuple[Any, float]]:
        """按技能重叠度打分并降序返回 [(AgentSpec, score)]。"""
        wanted = set(skills or [])
        scored: list[tuple[Any, float]] = []
        for spec in self.specs:
            overlap = len(wanted & set(getattr(spec, "skills", []) or []))
            score = 0.6 + min(0.4, 0.1 * overlap) if wanted else 0.6
            scored.append((spec, round(score, 4)))
        return sorted(scored, key=lambda item: -item[1])


def agent_result(
    output: Any,
    confidence: float = 0.9,
    *,
    error: str | None = None,
    tokens_in: int = 3,
    tokens_out: int = 5,
    latency_s: float = 0.001,
) -> AgentResult:
    """构造带默认 token / 延迟统计的 AgentResult。"""
    return AgentResult(
        output=output,
        confidence=confidence,
        error=error,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_s=latency_s,
    )


class FakeVerifier:
    """按预设序列返回验证结果，并模拟流水线的 ``run.verification`` 记录。"""

    def __init__(self, results: Iterable[bool | VerifyResult]) -> None:
        self._queue: list[bool | VerifyResult] = list(results)
        self.calls: list[dict[str, Any]] = []

    async def verify(
        self,
        spec: Any,
        node: Any,
        output: Any,
        run: RunState,
    ) -> VerifyResult:
        """返回下一个预设结果；队列耗尽后默认通过。"""
        self.calls.append(
            {
                "node_id": getattr(node, "id", ""),
                "output": output,
                "spec": spec,
            }
        )
        nxt: bool | VerifyResult = self._queue.pop(0) if self._queue else True
        if isinstance(nxt, VerifyResult):
            result = nxt
        else:
            result = VerifyResult(
                passed=bool(nxt),
                confidence=0.9 if nxt else 0.5,
                checks=[
                    VerifyCheckResult(
                        name="schema",
                        passed=bool(nxt),
                        detail="输出结构完整" if nxt else "缺少 metrics 字段",
                        evidence={
                            "expected": {"metrics": "dict"},
                            "actual": {"metrics": "missing"},
                            "suggestion": "补齐 metrics 后重新输出 JSON",
                        }
                        if not nxt
                        else {},
                    )
                ],
                feedback="" if nxt else "【验证未通过】缺少 metrics 字段，请补齐后重新输出 JSON。",
            )
        run.add_verification(
            {
                "node_id": getattr(node, "id", ""),
                "ts": time.time(),
                "passed": result.passed,
                "confidence": result.confidence,
                "layer": result.layer,
                "checks": [check.to_dict() for check in result.checks],
                "feedback": result.feedback,
                "metrics": dict(result.metrics),
            }
        )
        return result


class FakeCtx:
    """RuntimeContext 的轻量替身，字段与真实上下文保持同名。"""

    def __init__(
        self,
        agent_fn: Callable[..., Any] | None = None,
        tool_fn: Callable[..., Any] | None = None,
        *,
        verifier: Any = None,
        scheduler: Any = None,
        human_decision: bool = True,
        config: dict[str, Any] | None = None,
        agents: Iterable[Any] | None = None,
        auto_approve: bool = True,
    ) -> None:
        self.events: list[SimpleNamespace] = []
        self.logs: list[dict[str, Any]] = []
        self.agent_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.human_calls: list[str] = []
        self.config = dict(config or {})
        self.cancel_event = threading.Event()
        self.verifier = verifier
        self.scheduler = scheduler
        self.agents = FakeAgents(agents)
        self.workflows: dict[str, Any] = {}
        self.llm = None
        self.tools: Any = SimpleNamespace() if tool_fn is not None else None
        self.auto_approve = auto_approve
        self.human_decision = human_decision
        self.run_id = ""
        self.artifacts_dir = ""
        self.workspace_dir = ""
        self.platform = None
        self._agent_fn = agent_fn or (
            lambda agent_id, prompt, node, run, feedback, index: agent_result(
                {"agent": agent_id, "prompt": prompt}
            )
        )
        self._tool_fn = tool_fn or (
            lambda name, args: ToolResult.failure(f"未配置工具: {name}")
        )

    # ------------------------------------------------------------------
    # 事件 / 日志
    # ------------------------------------------------------------------
    def emit(
        self,
        type_: str,
        run_id: str | None = None,
        node_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> SimpleNamespace:
        """同步记录事件（真实 EventBus 也是同步 emit）。"""
        event = SimpleNamespace(
            type=type_,
            run_id=run_id,
            node_id=node_id,
            agent_id=agent_id,
            payload=dict(payload or {}),
            level=level,
        )
        self.events.append(event)
        return event

    def log(
        self,
        run_id: str,
        message: str,
        level: str = "info",
        **kwargs: Any,
    ) -> SimpleNamespace:
        """记录一条日志事件。"""
        return self.emit("log", run_id=run_id, payload={"message": message, **kwargs})

    # ------------------------------------------------------------------
    # 外部能力桩
    # ------------------------------------------------------------------
    async def call_agent(
        self,
        agent_id: str,
        prompt: str,
        run: RunState,
        node: Any,
        context: dict[str, Any] | None = None,
        feedback: str = "",
        node_state: Any = None,
    ) -> AgentResult:
        """调用可编程 Agent 回调并累加 token 统计。"""
        index = len(self.agent_calls)
        self.agent_calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "node_id": getattr(node, "id", ""),
                "feedback": feedback,
                "context": dict(context or {}),
                "index": index,
            }
        )
        result = self._agent_fn(agent_id, prompt, node, run, feedback, index)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, AgentResult):
            result = agent_result(result)
        if node_state is not None:
            node_state.tokens_in = int(getattr(node_state, "tokens_in", 0)) + result.tokens_in
            node_state.tokens_out = int(getattr(node_state, "tokens_out", 0)) + result.tokens_out
        return result

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """调用可编程工具回调。"""
        self.tool_calls.append({"name": name, "args": dict(args or {})})
        result = self._tool_fn(name, args)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, ToolResult):
            result = ToolResult.success(result)
        return result

    async def human_gate(self, node: Any, run: RunState) -> bool:
        """人工闸门桩：返回预设决策。"""
        self.human_calls.append(getattr(node, "id", ""))
        await asyncio.sleep(0)
        return bool(self.human_decision)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def is_cancelled(self) -> bool:
        """是否已请求取消。"""
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        """请求取消。"""
        self.cancel_event.set()


def make_run(
    workflow: Any,
    inputs: dict[str, Any] | None = None,
    *,
    run_id: str = "run-test",
    max_steps: int = 200,
) -> RunState:
    """构造与工作流绑定的 RunState。"""
    return RunState(
        run_id=run_id,
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        inputs=dict(inputs or {}),
        max_steps=max_steps,
    )


def execute_workflow(
    workflow: Any,
    ctx: FakeCtx,
    inputs: dict[str, Any] | None = None,
    *,
    run: RunState | None = None,
    max_steps: int = 200,
) -> tuple[Any, RunState]:
    """在独立事件循环中执行工作流，返回 (EngineResult, RunState)。"""
    active_run = run or make_run(workflow, inputs, max_steps=max_steps)
    engine = WorkflowEngine(ctx)
    result = asyncio.run(engine.execute(workflow, active_run, inputs))
    return result, active_run


def events_of(ctx: FakeCtx, type_: str) -> list[SimpleNamespace]:
    """按事件类型过滤事件。"""
    return [event for event in ctx.events if event.type == type_]
