"""运行时上下文：执行引擎与具体实现之间的唯一桥梁。

执行引擎只认识 ``RuntimeContext``，不直接 import LLM、Agent、工具实现，
因此可以用 Mock 实现做单元测试，也可以在运行中热切换模型提供方。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core import events as events_mod
from ..core.types import AgentResult, AgentSpec, EventType, NodeState, RunStatus, ToolCallRecord


@dataclass
class RuntimeContext:
    """一次运行（或一次引擎调用）所共享的依赖集合。"""

    bus: events_mod.EventBus
    llm: Any = None
    tools: Any = None
    agents: Any = None
    verifier: Any = None
    scheduler: Any = None
    workflows: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    artifacts_dir: str = ""
    workspace_dir: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    auto_approve: bool = True
    run_id: str = ""
    platform: Any = None

    # 人工审批：{f"{run_id}:{node_id}": threading.Event}
    _approvals: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    _approval_results: dict[str, bool] = field(default_factory=dict, repr=False)

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
    ) -> None:
        self.bus.emit(
            type_,
            run_id=run_id or self.run_id,
            node_id=node_id,
            agent_id=agent_id,
            payload=payload or {},
            level=level,
        )

    def log(self, run_id: str | None, message: str, level: str = "info", **kw: Any) -> None:
        self.emit(EventType.LOG, run_id=run_id, payload={"message": message, **kw}, level=level)

    # ------------------------------------------------------------------
    # Agent / 工具调用
    # ------------------------------------------------------------------
    async def call_agent(
        self,
        agent_id: str,
        prompt: str,
        run: Any,
        node: Any,
        context: dict[str, Any] | None = None,
        feedback: str = "",
        node_state: NodeState | None = None,
    ) -> AgentResult:
        """执行一个 Agent 任务，并把 Token/延迟记账到节点状态。"""
        from ..agents.agent import AgentRuntime
        from ..agents.memory import AgentMemory

        spec: AgentSpec | None = self.agents.get(agent_id) if self.agents else None
        if spec is None:
            return AgentResult(output=None, error=f"Agent 不存在: {agent_id}", confidence=0.0)

        acquired = self.agents.acquire(agent_id) if hasattr(self.agents, "acquire") else True
        try:
            memory = AgentMemory(run_id=run.run_id)
            # 运行级记忆：把已完成的节点结论作为上下文，支持 Agent 间协同
            for nid, st in getattr(run, "node_states", {}).items():
                if st.status == "succeeded" and st.output is not None:
                    memory.note(f"[{nid}] {str(st.output)[:1200]}", tags=[nid])

            runtime = AgentRuntime(
                spec=spec,
                llm=self.llm,
                tools=self.tools,
                bus=self.bus,
                memory=memory,
                run_id=run.run_id,
            )
            result = await runtime.run(
                prompt=prompt,
                run_id=run.run_id,
                node_id=node.id,
                context=context or {},
                feedback=feedback,
            )
            if node_state is not None:
                node_state.tokens_in += result.tokens_in
                node_state.tokens_out += result.tokens_out
                node_state.agent_id = agent_id
            return result
        finally:
            if hasattr(self.agents, "release"):
                self.agents.release(agent_id)
            if not acquired and hasattr(self.agents, "acquire"):
                self.log(run.run_id, f"Agent {agent_id} 并发已满，本次调用排队完成", level="warning")

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """调用工具并返回统一 ToolResult（鸭子类型：ok/value/error）。"""
        if self.tools is None:
            raise RuntimeError("工具注册表未初始化")
        return await self.tools.call(name, args or {}, ctx=self)

    # ------------------------------------------------------------------
    # 人工审批闸门
    # ------------------------------------------------------------------
    async def human_gate(self, node: Any, run: Any) -> bool:
        key = f"{run.run_id}:{node.id}"
        payload = {
            "node_id": node.id,
            "label": getattr(node, "label", "") or node.id,
            "message": getattr(node, "description", "") or "请确认是否继续执行",
        }
        self.emit(EventType.HUMAN_REQUIRED, node_id=node.id, payload=payload)
        run.status = RunStatus.PAUSED.value

        if self.auto_approve:
            await asyncio.sleep(0.05)
            self._approval_results[key] = True
            self.emit(
                EventType.HUMAN_DECIDED,
                node_id=node.id,
                payload={"approved": True, "auto": True},
            )
            run.status = RunStatus.RUNNING.value
            return True

        ev = threading.Event()
        self._approvals[key] = ev
        timeout = float(getattr(node, "timeout_s", 120.0) or 120.0)
        while not ev.wait(0.2):
            if self.cancel_event.is_set():
                run.status = RunStatus.RUNNING.value
                return False
            elapsed = time.time() - run.started_at if run.started_at else 0.0
            if elapsed > max(timeout, 600.0):
                self.emit(
                    EventType.HUMAN_DECIDED,
                    node_id=node.id,
                    payload={"approved": False, "reason": "timeout"},
                    level="warning",
                )
                run.status = RunStatus.RUNNING.value
                return False
        approved = bool(self._approval_results.get(key, False))
        self.emit(EventType.HUMAN_DECIDED, node_id=node.id, payload={"approved": approved})
        run.status = RunStatus.RUNNING.value
        return approved

    def resolve_approval(self, run_id: str, node_id: str, approved: bool) -> bool:
        key = f"{run_id}:{node_id}"
        ev = self._approvals.get(key)
        if ev is None:
            return False
        self._approval_results[key] = bool(approved)
        ev.set()
        return True

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()
