"""运行状态与运行仓库（RunState / RunStore）。

设计目标
--------
* ``RunState`` 是一次运行的共享黑板：引擎写节点状态与变量，编排器/API 读摘要；
* 引擎只在 asyncio 单线程内写，但 Web 线程会并发读取，因此所有读写都加 ``RLock``；
* 支持运行级取消信号、状态快照/回滚、指标汇总，供「运行监控」页与失败恢复使用。

线程模型：``RunStore`` 可被 HTTP 线程与执行线程同时访问；``RunState`` 的
``cancel_event`` 是跨线程取消的唯一权威信号（与 ``RuntimeContext.cancel_event`` 并列）。
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any

from ..core.types import (
    NodeState,
    NodeStatus,
    RunRequest,
    RunStatus,
    RunSummary,
    Workflow,
    new_id,
)

_TERMINAL = {
    NodeStatus.SUCCEEDED.value,
    NodeStatus.FAILED.value,
    NodeStatus.SKIPPED.value,
}
_ACTIVE = {
    NodeStatus.SCHEDULED.value,
    NodeStatus.RUNNING.value,
    NodeStatus.RETRYING.value,
    NodeStatus.WAITING_HUMAN.value,
}


class RunState:
    """一次运行的可变状态容器（引擎写入、API 读取）。"""

    def __init__(
        self,
        run_id: str,
        workflow_id: str,
        workflow_name: str = "",
        label: str = "",
        policy: str = "capability",
        inputs: dict[str, Any] | None = None,
        max_steps: int = 500,
        artifacts_dir: str = "",
    ) -> None:
        self.run_id: str = run_id
        self.workflow_id: str = workflow_id
        self.workflow_name: str = workflow_name
        self.label: str = label
        self.status: str = RunStatus.PENDING.value
        self.policy: str = policy
        self.variables: dict[str, Any] = {}
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.node_states: dict[str, NodeState] = {}
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.created_at: float = time.time()
        self.error: str = ""
        self.self_loop: dict[str, Any] = {}
        self.verification: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.max_steps: int = max(1, int(max_steps))
        self.artifacts_dir: str = artifacts_dir
        self.cancel_event: threading.Event = threading.Event()
        self.lock: threading.RLock = threading.RLock()
        self._snapshots: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 变量与节点状态
    # ------------------------------------------------------------------
    def set_var(self, key: str, value: Any) -> None:
        """写入一个运行级变量（线程安全）。"""
        with self.lock:
            self.variables[key] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        """读取运行级变量。"""
        with self.lock:
            return self.variables.get(key, default)

    def ensure_node(self, node_id: str) -> NodeState:
        """取得（不存在则创建）节点状态对象。"""
        with self.lock:
            state = self.node_states.get(node_id)
            if state is None:
                state = NodeState(node_id=node_id)
                self.node_states[node_id] = state
            return state

    def set_node_status(self, node_id: str, status: str, **fields: Any) -> NodeState:
        """更新节点状态；终止态自动补 ``finished_at``。"""
        state = self.ensure_node(node_id)
        with self.lock:
            state.status = status
            for key, value in fields.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            if status in _TERMINAL and not state.finished_at:
                state.finished_at = time.time()
        return state

    def node_state(self, node_id: str) -> NodeState | None:
        with self.lock:
            return self.node_states.get(node_id)

    # ------------------------------------------------------------------
    # 快照 / 回滚
    # ------------------------------------------------------------------
    def snapshot(self, label: str = "") -> int:
        """保存一份状态快照并返回索引（供失败分支重放/回滚）。"""
        with self.lock:
            self._snapshots.append(
                {
                    "label": label or f"snapshot-{len(self._snapshots) + 1}",
                    "ts": time.time(),
                    "status": self.status,
                    "variables": copy.deepcopy(self.variables),
                    "node_states": copy.deepcopy(self.node_states),
                }
            )
            return len(self._snapshots) - 1

    def rollback(self, index: int) -> None:
        """回滚到指定快照；索引越界抛 ``IndexError``。"""
        with self.lock:
            if index < 0 or index >= len(self._snapshots):
                raise IndexError(f"快照索引越界: {index}")
            snap = self._snapshots[index]
            self.status = snap["status"]
            self.variables = copy.deepcopy(snap["variables"])
            self.node_states = copy.deepcopy(snap["node_states"])
            self._snapshots = self._snapshots[: index + 1]

    @property
    def snapshot_count(self) -> int:
        with self.lock:
            return len(self._snapshots)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """请求取消本次运行（可跨线程调用）。"""
        self.cancel_event.set()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    # ------------------------------------------------------------------
    # 验证 / 循环 / 产物
    # ------------------------------------------------------------------
    def add_verification(self, record: dict[str, Any]) -> None:
        """追加一条验证记录（前端验证中心按序展示）。"""
        with self.lock:
            self.verification.append(record)

    def set_self_loop(self, node_id: str, summary: dict[str, Any]) -> None:
        with self.lock:
            self.self_loop[node_id] = summary

    def add_artifact(self, name: str, path: str, kind: str = "file") -> None:
        with self.lock:
            self.artifacts.append(
                {"name": name, "path": path, "kind": kind, "ts": time.time()}
            )

    # ------------------------------------------------------------------
    # 指标 / 摘要
    # ------------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        """运行级指标：节点计数、成功率、耗时、Token、循环轮数、平均置信度。"""
        with self.lock:
            states = list(self.node_states.values())
            succeeded = sum(1 for s in states if s.status == NodeStatus.SUCCEEDED.value)
            failed = sum(1 for s in states if s.status == NodeStatus.FAILED.value)
            skipped = sum(1 for s in states if s.status == NodeStatus.SKIPPED.value)
            active = sum(1 for s in states if s.status in _ACTIVE)
            pending = sum(1 for s in states if s.status == NodeStatus.PENDING.value)
            executed = succeeded + failed
            success_rate = round(succeeded / executed, 4) if executed else 0.0
            tokens_in = sum(s.tokens_in for s in states)
            tokens_out = sum(s.tokens_out for s in states)
            confidences = [
                s.confidence for s in states if s.status == NodeStatus.SUCCEEDED.value
            ]
            avg_confidence = (
                round(sum(confidences) / len(confidences), 4) if confidences else 0.0
            )
            loop_iterations = 0
            for summary in self.self_loop.values():
                if isinstance(summary, dict):
                    loop_iterations += int(
                        summary.get("iteration", summary.get("iterations", 0)) or 0
                    )
            duration = 0.0
            if self.started_at:
                end = self.finished_at or time.time()
                duration = round(max(0.0, end - self.started_at), 3)
            return {
                "nodes_total": len(states),
                "nodes_succeeded": succeeded,
                "nodes_failed": failed,
                "nodes_skipped": skipped,
                "nodes_active": active,
                "nodes_pending": pending,
                "success_rate": success_rate,
                "duration_s": duration,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_total": tokens_in + tokens_out,
                "self_loop_iterations": loop_iterations,
                "verification_count": len(self.verification),
                "avg_confidence": avg_confidence,
            }

    def to_summary(self) -> RunSummary:
        """构造对外 ``RunSummary`` 数据对象。"""
        with self.lock:
            return RunSummary(
                run_id=self.run_id,
                workflow_id=self.workflow_id,
                workflow_name=self.workflow_name,
                label=self.label,
                status=self.status,
                created_at=self.created_at,
                started_at=self.started_at,
                finished_at=self.finished_at,
                policy=self.policy,
                nodes=copy.deepcopy(self.node_states),
                variables=self.variables,
                inputs=dict(self.inputs),
                error=self.error,
                metrics=self.metrics(),
                verification=list(self.verification),
                self_loop=dict(self.self_loop),
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化摘要（API 直接返回）。"""
        return self.to_summary().to_dict()


class RunStore:
    """有界的运行仓库（线程安全，供 API 与编排器共享）。"""

    def __init__(self, max_runs: int = 200) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, RunState] = {}
        self._order: list[str] = []
        self._orphan_flags: dict[str, threading.Event] = {}
        self._max_runs = max(1, int(max_runs))

    # ------------------------------------------------------------------
    def create(self, req: RunRequest, wf: Workflow) -> RunState:
        """创建运行状态并登记（超出上限时淘汰最老的已结束运行）。"""
        run_id = new_id("run-")
        run = RunState(
            run_id=run_id,
            workflow_id=wf.id,
            workflow_name=wf.name,
            label=req.label,
            policy=req.policy,
            inputs=dict(req.inputs),
            max_steps=req.max_steps,
        )
        with self._lock:
            self._runs[run_id] = run
            self._order.append(run_id)
            self._evict_locked()
        return run

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """按创建时间倒序返回运行摘要。"""
        with self._lock:
            run_ids = list(reversed(self._order))[: max(1, int(limit))]
            items: list[dict[str, Any]] = []
            for run_id in run_ids:
                run = self._runs.get(run_id)
                if run is not None:
                    items.append(run.to_dict())
            return items

    def finish(self, run: RunState, status: str, error: str = "") -> None:
        """标记运行结束。"""
        with self._lock:
            run.status = status
            run.error = error
            run.finished_at = time.time()

    def cancel_flags(self, run_id: str) -> threading.Event:
        """返回取消信号；运行不存在时返回独立旗标（幂等，供 API 使用）。"""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                return run.cancel_event
            flag = self._orphan_flags.get(run_id)
            if flag is None:
                flag = threading.Event()
                self._orphan_flags[run_id] = flag
            return flag

    def to_summary(self, run: RunState) -> dict[str, Any]:
        """导出单次运行摘要（优先使用运行自带快照）。"""
        with self._lock:
            return run.to_dict()

    # ------------------------------------------------------------------
    def _evict_locked(self) -> None:
        """仓库超过上限时，从最老的已结束运行开始淘汰。"""
        while len(self._order) > self._max_runs:
            victim: str | None = None
            for run_id in self._order:
                run = self._runs.get(run_id)
                if run is None or run.status in (
                    RunStatus.SUCCEEDED.value,
                    RunStatus.FAILED.value,
                    RunStatus.CANCELLED.value,
                    RunStatus.REJECTED.value,
                ):
                    victim = run_id
                    break
            if victim is None:
                # 全部在运行：淘汰最老的一条，保证内存有界
                victim = self._order[0]
            self._order.remove(victim)
            self._runs.pop(victim, None)
            self._orphan_flags.pop(victim, None)
