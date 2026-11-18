"""工作流执行引擎：拓扑推进、分层并发、条件分支、循环与验证闭环。

设计要点
--------
* **真并发**：同一波就绪节点用 ``asyncio.gather`` 并行执行，并用
  ``Semaphore`` 限制同时运行的叶子节点数（``max_concurrent``）；结构型节点
  （loop/subworkflow）不占用信号量，避免「循环体等待外层槽位」的死锁；
* **真验证**：有 ``verify`` 配置的节点调用 ``VerificationPipeline``；验证失败
  且 ``allow_self_repair`` 时由 ``SelfLoopController`` 驱动多轮修复，把结构化
  中文反馈写入 ``feedback`` 变量并注入下一次 prompt；
* **真反馈**：循环节点每轮把验证反馈写入 ``LoopSpec.feedback_var``，循环体
  重跑时通过 ``{{feedback}}`` 模板读取，实现「验证—反馈—重算」的 PDCA 闭环；
* **可观测**：节点生命周期、调度得分/理由、循环轮次、验证结果全部落到事件
  总线，供前端运行监控与验证中心直接消费；
* **可取消**：每个调度波次与节点边界检查取消标志，取消后返回
  ``EngineResult(status="cancelled")``，绝不吞掉 ``asyncio.CancelledError``。

节点类型语义
------------
start/end     占位节点，输出运行输入/结束标记；
agent         通过 ``ctx.call_agent`` 调用 Agent，prompt 支持 ``{{var}}`` 模板；
tool          通过 ``ctx.call_tool`` 调用工具，args 递归渲染模板；
condition     按边声明顺序求值 ``edge.condition``，取第一条为真的出边；没有
              条件为真时走「无 condition 的默认边」；被放弃的分支整条标记 skipped；
loop          执行 ``node.loop.body`` 指定的子图（缺省时自动发现回边子图），
              每轮发 ``loop.iteration``，验证通过或达到上限后发 ``loop.exit``；
subworkflow   在子 ``RunState`` 中递归执行另一条工作流，再合并回父运行；
verify        对前驱输出执行验证流水线，结果写 ``run.verification``；
human_gate    调用 ``ctx.human_gate``，拒绝时按 ``on_failure`` 处理；
join          汇聚前驱输出，供后续节点做汇总。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.expr import ExprError, render_template, truthy
from ..core.types import (
    EventType,
    LoopSpec,
    NodeStatus,
    NodeType,
    RetryPolicy,
    RunStatus,
    VerifyCheckResult,
    VerifyResult,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from ..loop.selfloop import SelfLoopController
from ..runtime.state import RunState
from ..scheduler.engine import ScheduleCandidate

log = logging.getLogger("agentweave.workflow.engine")

#: 子工作流最大递归深度（防御配置错误导致的无限嵌套）
MAX_SUBWORKFLOW_DEPTH = 6
#: 单节点事件中输出预览的最大字符数
PREVIEW_LIMIT = 600


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class EngineResult:
    """执行引擎的统一返回值。"""

    status: str = RunStatus.SUCCEEDED.value
    error: str = ""
    variables: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为字典（便于 API 序列化）。"""
        return {"status": self.status, "error": self.error, "variables": self.variables}


class NodeExecutionError(RuntimeError):
    """节点执行失败；``verify`` 携带导致失败的验证结果（若有）。"""

    def __init__(
        self,
        message: str,
        verify: VerifyResult | None = None,
        output: Any = None,
    ) -> None:
        super().__init__(message)
        self.verify = verify
        self.output = output


class _CancelRun(Exception):
    """内部信号：运行被取消。"""


class _AbortRun(Exception):
    """内部信号：按 ``on_failure=abort`` 终止整次运行。"""


@dataclass
class _NodeOutput:
    """单个节点一次执行的产出（内部结构）。"""

    output: Any = None
    confidence: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float = 0.0
    agent_id: str = ""
    verify: VerifyResult | None = None
    failed: bool = False
    error: str = ""
    #: condition 节点选中的目标节点列表；None 表示所有出边都激活
    branch_targets: list[str] | None = None


@dataclass
class _Outcome:
    """节点执行结果的结构化描述，供区域调度器推进边。"""

    ok: bool
    error: str = ""
    strategy: str = "abort"
    branch_targets: list[str] | None = None
    output: Any = None


@dataclass
class _SchedInfo:
    """一次调度的观测信息（得分、理由、负载）。"""

    node_id: str
    agent_id: str = ""
    score: float = 0.0
    reason: str = ""
    agent_load: float = 0.0


# ---------------------------------------------------------------------------
# 稳定排序用的边界
# ---------------------------------------------------------------------------
_OUT_STRATEGIES = {"abort", "continue", "skip"}


def _node_type(node: WorkflowNode) -> str:
    """把节点类型统一成字符串（兼容枚举/字符串两种存法）。"""
    value = getattr(node, "type", "")
    return value.value if isinstance(value, NodeType) else str(value)


def _preview(value: Any, limit: int = PREVIEW_LIMIT) -> str:
    """把任意输出压成短预览字符串（事件 payload 用）。"""
    try:
        import json

        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class WorkflowEngine:
    """基于 asyncio 的工作流执行引擎。"""

    #: 默认并发上限
    DEFAULT_MAX_CONCURRENT = 4

    def __init__(
        self,
        ctx: Any = None,
        max_steps: int | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        """构造引擎。

        参数
        ----
        ctx: ``RuntimeContext``；也可在 ``execute`` 时通过同名参数注入；
        max_steps: 单次运行的节点执行步数上限（防止配置死循环）；
        max_concurrent: 同一波次内并行叶子节点的上限。
        """
        self.ctx = ctx
        config = dict(getattr(ctx, "config", {}) or {}) if ctx is not None else {}
        scheduler_cfg = dict(config.get("scheduler") or {})
        self.max_steps = int(
            max_steps or config.get("max_steps") or scheduler_cfg.get("max_steps") or 500
        )
        self.max_concurrent = int(
            max_concurrent
            or config.get("max_concurrent")
            or scheduler_cfg.get("max_concurrent_nodes")
            or self.DEFAULT_MAX_CONCURRENT
        )
        self.max_concurrent = max(1, self.max_concurrent)
        self._semaphore: asyncio.Semaphore | None = None
        self._agent_loads: dict[str, int] = {}
        self._steps = 0
        self._plan_emitted = False
        self._run: RunState | None = None

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def execute(
        self,
        workflow: Workflow,
        run: RunState | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> EngineResult:
        """执行一条工作流并返回终态结果。

        参数
        ----
        workflow: 待执行的工作流定义；
        run: 运行状态容器（缺省时自动创建，便于单测直接调用）；
        inputs: 运行输入（会以 ``setdefault`` 方式合并，不覆盖已有变量）。
        """
        if self.ctx is None:
            raise RuntimeError("WorkflowEngine 缺少 RuntimeContext，无法执行工作流")
        if run is None:
            run = RunState(
                run_id=f"run-{int(time.time() * 1000)}",
                workflow_id=workflow.id,
                workflow_name=workflow.name,
            )
        self._run = run
        self._steps = 0
        self._plan_emitted = False
        self._agent_loads = {}
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        merged_inputs = dict(inputs or {})
        for key, value in merged_inputs.items():
            run.inputs.setdefault(key, value)
            run.variables.setdefault(key, value)
        for key, value in (workflow.variables or {}).items():
            run.variables.setdefault(key, value)
        if not run.started_at:
            run.started_at = time.time()
        if run.status in (RunStatus.PENDING.value, RunStatus.PAUSED.value):
            run.status = RunStatus.RUNNING.value

        self._emit_plan(run, workflow)
        error = ""
        status = RunStatus.SUCCEEDED.value
        try:
            await self._run_graph(workflow, run, depth=0)
        except _CancelRun:
            status = RunStatus.CANCELLED.value
            error = "运行已取消"
        except _AbortRun as exc:
            status = RunStatus.FAILED.value
            error = str(exc) or "工作流执行失败"
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED.value
            error = "运行已取消"
        except Exception as exc:  # pragma: no cover - 兜底，异常必须可见
            status = RunStatus.FAILED.value
            error = f"{type(exc).__name__}: {exc}"
            log.exception("工作流 %s 执行异常", workflow.id)
        else:
            cancelled = self._is_cancelled(run)
            failed_nodes = [
                node_id
                for node_id, state in run.node_states.items()
                if state.status == NodeStatus.FAILED.value
            ]
            if cancelled:
                status = RunStatus.CANCELLED.value
                error = "运行已取消"
            elif failed_nodes:
                status = RunStatus.FAILED.value
                error = "节点执行失败: " + ", ".join(failed_nodes[:5])
            elif getattr(run, "error", ""):
                status = RunStatus.FAILED.value
                error = str(run.error)
        run.status = status
        run.error = error
        run.finished_at = time.time()
        return EngineResult(status=status, error=error, variables=dict(run.variables))

    # 兼容旧调用名
    run_workflow = execute

    # ------------------------------------------------------------------
    # 图层执行
    # ------------------------------------------------------------------
    async def _run_graph(self, workflow: Workflow, run: RunState, depth: int = 0) -> None:
        """执行一条完整工作流的顶层节点集合。"""
        if depth > MAX_SUBWORKFLOW_DEPTH:
            raise _AbortRun(f"子工作流嵌套超过 {MAX_SUBWORKFLOW_DEPTH} 层: {workflow.id}")
        node_ids = [node.id for node in workflow.nodes]
        if not node_ids:
            return
        await self._execute_region(workflow, run, node_ids, depth=depth)

    async def _execute_region(
        self,
        workflow: Workflow,
        run: RunState,
        node_ids: list[str],
        depth: int = 0,
    ) -> None:
        """执行一个节点区域（整图或循环体），按拓扑分层并发推进。"""
        region_ids = self._expand_region(workflow, node_ids)
        if not region_ids:
            return
        owners = self._owner_map(workflow)
        order = self._region_order(workflow, region_ids)
        region_set = set(region_ids)
        out_map: dict[str, list[tuple[WorkflowEdge, str]]] = {nid: [] for nid in region_ids}
        dep_count: dict[str, int] = {nid: 0 for nid in region_ids}
        in_active: dict[str, int] = {nid: 0 for nid in region_ids}
        in_edges: dict[str, int] = {nid: 0 for nid in region_ids}
        for edge in workflow.edges:
            if edge.kind == "loop_back":
                continue
            source = edge.source
            if source not in region_set:
                source = owners.get(source, source)
            if source not in region_set or edge.target not in region_set:
                continue
            if source == edge.target:
                continue
            out_map[source].append((edge, edge.target))
            dep_count[edge.target] += 1
            in_edges[edge.target] += 1
        entry = {nid for nid in region_ids if in_edges[nid] == 0}
        pending = set(region_ids)

        def deactivate(node_id: str, reason: str) -> None:
            """把节点标记为跳过，并释放其所有出边的依赖计数。"""
            pending.discard(node_id)
            run.set_node_status(node_id, NodeStatus.SKIPPED.value, error=reason)
            self._emit(
                run,
                EventType.NODE_SKIPPED,
                node_id=node_id,
                payload={"node_id": node_id, "reason": reason},
            )
            for _edge, target in out_map.get(node_id, []):
                dep_count[target] -= 1

        while pending:
            self._check_cancelled(run)
            # 1) 先处理「所有入边都被条件分支放弃」的节点（级联跳过）
            changed = True
            while changed:
                changed = False
                for node_id in list(pending):
                    if dep_count[node_id] <= 0 and node_id not in entry and in_active[node_id] <= 0:
                        deactivate(node_id, "上游条件分支未选中")
                        changed = True
            # 2) 收集就绪节点
            ready = [
                node_id
                for node_id in order
                if node_id in pending
                and dep_count[node_id] <= 0
                and (node_id in entry or in_active[node_id] > 0)
            ]
            if not ready:
                if pending:
                    raise _AbortRun(
                        "拓扑推进死锁，未调度节点: " + ", ".join(sorted(pending)[:8])
                    )
                break
            # 3) 调度并并发执行这一波
            scheduled = self._schedule_wave(run, workflow, ready)
            self._steps += len(scheduled)
            if self._steps > self.max_steps:
                raise _AbortRun(f"节点执行步数超过上限 {self.max_steps}")
            tasks = [
                asyncio.ensure_future(self._process_node(workflow, run, item, depth=depth))
                for item in scheduled
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for item, result in zip(scheduled, results):
                node = workflow.node(item.node_id)
                pending.discard(node.id)
                if isinstance(result, BaseException):
                    if isinstance(result, (_CancelRun, _AbortRun, asyncio.CancelledError)):
                        raise result
                    error = f"{type(result).__name__}: {result}"
                    run.set_node_status(node.id, NodeStatus.FAILED.value, error=error)
                    self._emit(
                        run,
                        EventType.NODE_FAILED,
                        node_id=node.id,
                        payload={"node_id": node.id, "error": error, "attempt": 1},
                        level="error",
                    )
                    outcome = _Outcome(ok=False, error=error, strategy="abort")
                else:
                    outcome = result
                # 4) 按结果激活/放弃出边
                for _edge, target in out_map.get(node.id, []):
                    active = True
                    if outcome.branch_targets is not None:
                        active = target in outcome.branch_targets
                    if not outcome.ok and outcome.strategy == "skip":
                        active = False
                    if not outcome.ok and outcome.strategy == "abort":
                        raise _AbortRun(outcome.error or f"节点 {node.id} 执行失败")
                    dep_count[target] -= 1
                    if active:
                        in_active[target] += 1

    def _expand_region(self, workflow: Workflow, node_ids: list[str]) -> list[str]:
        """去掉区域中「已由 loop 节点托管」的循环体节点。"""
        ordered = list(dict.fromkeys(node_ids))
        excluded: set[str] = set()
        for node_id in ordered:
            try:
                node = workflow.node(node_id)
            except KeyError:  # pragma: no cover - 防御脏数据
                continue
            if _node_type(node) == NodeType.LOOP.value:
                excluded.update(self._loop_body(workflow, node))
        return [node_id for node_id in ordered if node_id not in excluded]

    def _region_order(self, workflow: Workflow, region_ids: list[str]) -> list[str]:
        """返回区域内稳定的拓扑顺序；拓扑异常时回退为声明顺序。"""
        try:
            topological = workflow.topological_order()
        except ValueError:
            topological = []
        region_set = set(region_ids)
        ordered = [node_id for node_id in topological if node_id in region_set]
        seen = set(ordered)
        ordered.extend(node_id for node_id in region_ids if node_id not in seen)
        return ordered

    def _owner_map(self, workflow: Workflow) -> dict[str, str]:
        """循环体节点 -> 所属 loop 节点 id 的映射。"""
        owners: dict[str, str] = {}
        for node in workflow.nodes:
            if _node_type(node) != NodeType.LOOP.value:
                continue
            for body_id in self._loop_body(workflow, node):
                owners[body_id] = node.id
        return owners

    # ------------------------------------------------------------------
    # 循环体识别
    # ------------------------------------------------------------------
    def _loop_body(self, workflow: Workflow, node: WorkflowNode) -> list[str]:
        """返回 loop 节点的循环体节点 id 列表（优先显式 ``body``）。"""
        spec = node.loop or LoopSpec()
        known = {item.id for item in workflow.nodes}
        if spec.body:
            body = [body_id for body_id in spec.body if body_id in known and body_id != node.id]
            body = list(dict.fromkeys(body))
            if body:
                return body
        return self._discover_loop_body(workflow, node)

    def _discover_loop_body(self, workflow: Workflow, node: WorkflowNode) -> list[str]:
        """从 loop 节点出发发现「有回边回到 loop 节点」的子图（兜底逻辑）。"""
        back_sources = {
            edge.source
            for edge in workflow.edges
            if edge.kind == "loop_back" and edge.target == node.id
        }
        reachable: list[str] = []
        seen = {node.id}
        frontier = [node.id]
        while frontier:
            current = frontier.pop(0)
            for edge in workflow.edges:
                if edge.source != current or edge.kind == "loop_back":
                    continue
                if edge.target in seen:
                    continue
                seen.add(edge.target)
                reachable.append(edge.target)
                frontier.append(edge.target)
        if not back_sources:
            return reachable
        reachable_set = set(reachable)
        keep = {source for source in back_sources if source in reachable_set}
        frontier = list(keep)
        while frontier:
            current = frontier.pop()
            for edge in workflow.edges:
                if edge.target != current or edge.kind == "loop_back":
                    continue
                if edge.source in reachable_set and edge.source not in keep:
                    keep.add(edge.source)
                    frontier.append(edge.source)
        return [node_id for node_id in reachable if node_id in keep]

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def _build_candidate(self, run: RunState, node: WorkflowNode) -> ScheduleCandidate:
        """为就绪节点构造调度候选（能力、负载、优先级、成本）。"""
        priority = _safe_float(node.config.get("priority"), 0.0)
        agent_id = self._resolve_agent(node)
        load = 0.0
        if agent_id:
            load = min(1.0, self._agent_loads.get(agent_id, 0) / max(1, self.max_concurrent))
        capability = self._capability(node, agent_id)
        return ScheduleCandidate(
            node=node,
            priority=priority,
            agent_id=agent_id,
            agent_load=load,
            capability=capability,
            est_cost=max(0.05, float(node.timeout_s or 1.0) / 120.0),
            data_ready=True,
        )

    def _schedule_wave(
        self, run: RunState, workflow: Workflow, ready: list[str]
    ) -> list[_SchedInfo]:
        """对一波就绪节点排序并产出 ``node.scheduled`` 观测信息。"""
        candidates = [self._build_candidate(run, workflow.node(node_id)) for node_id in ready]
        scheduler = getattr(self.ctx, "scheduler", None)
        chosen: list[tuple[ScheduleCandidate, float, str]] = []
        remaining = list(candidates)
        limit = min(self.max_concurrent, len(candidates))
        if scheduler is not None and hasattr(scheduler, "pick"):
            while remaining and len(chosen) < limit:
                try:
                    decision = scheduler.pick(remaining)
                except Exception:  # pragma: no cover - 调度器异常不阻断执行
                    log.debug("调度器 pick 失败，回退优先级排序", exc_info=True)
                    decision = None
                if decision is None:
                    break
                node_id = str(getattr(decision, "node_id", "") or "")
                picked = next(
                    (candidate for candidate in remaining if candidate.node.id == node_id),
                    remaining[0],
                )
                chosen.append(
                    (
                        picked,
                        float(getattr(decision, "score", 0.0) or 0.0),
                        str(getattr(decision, "reason", "") or ""),
                    )
                )
                remaining.remove(picked)
        if not chosen:  # 无调度器 / 调度器未给出结果：按优先级 + 声明序
            ordered = sorted(
                candidates,
                key=lambda candidate: (-candidate.priority, ready.index(candidate.node.id)),
            )
            for candidate in ordered[:limit]:
                score = round(
                    0.4 * candidate.capability
                    + 0.3 * (1.0 - candidate.agent_load)
                    + 0.2 * min(1.0, max(0.0, candidate.priority / 10.0))
                    + 0.1 * 0.5,
                    4,
                )
                reason = (
                    f"无调度器回退：能力 {candidate.capability:.2f}、"
                    f"空闲 {1.0 - candidate.agent_load:.2f}、优先级 {candidate.priority:.1f}"
                )
                chosen.append((candidate, score, reason))
        infos: list[_SchedInfo] = []
        for candidate, score, reason in chosen:
            info = _SchedInfo(
                node_id=candidate.node.id,
                agent_id=candidate.agent_id,
                score=score,
                reason=reason or "拓扑就绪",
                agent_load=candidate.agent_load,
            )
            infos.append(info)
        return infos

    def _emit_plan(self, run: RunState, workflow: Workflow) -> None:
        """广播一次运行的整体调度计划（``plan.ready``）。"""
        if self._plan_emitted:
            return
        self._plan_emitted = True
        scheduler = getattr(self.ctx, "scheduler", None)
        candidates: list[dict[str, Any]] = []
        for node in workflow.nodes:
            candidate = self._build_candidate(run, node)
            score = round(0.4 * candidate.capability + 0.3 * (1.0 - candidate.agent_load), 4)
            reason = (
                f"能力 {candidate.capability:.2f}、负载 {candidate.agent_load:.2f}、"
                f"优先级 {candidate.priority:.1f}"
            )
            if scheduler is not None and hasattr(scheduler, "pick"):
                try:
                    decision = scheduler.pick([candidate])
                except Exception:  # pragma: no cover
                    decision = None
                if decision is not None:
                    score = float(getattr(decision, "score", score) or score)
                    reason = str(getattr(decision, "reason", reason) or reason)
            candidates.append(
                {
                    "node_id": node.id,
                    "priority": candidate.priority,
                    "agent_id": candidate.agent_id,
                    "score": round(score, 4),
                    "reason": reason,
                }
            )
        policy = str(
            getattr(scheduler, "policy", "") or getattr(run, "policy", "") or "capability"
        )
        self._emit(
            run,
            EventType.PLAN_READY,
            payload={"policy": policy, "candidates": candidates},
        )

    # ------------------------------------------------------------------
    # 单节点执行（含重试）
    # ------------------------------------------------------------------
    async def _process_node(
        self, workflow: Workflow, run: RunState, item: _SchedInfo, depth: int = 0
    ) -> _Outcome:
        """执行一个节点：调度状态 -> 运行 -> 重试 -> 成功/失败。"""
        node = workflow.node(item.node_id)
        agent_id = item.agent_id or self._resolve_agent(node)
        run.set_node_status(
            node.id,
            NodeStatus.SCHEDULED.value,
            agent_id=agent_id,
        )
        self._emit(
            run,
            EventType.NODE_SCHEDULED,
            node_id=node.id,
            agent_id=agent_id or None,
            payload={
                "node_id": node.id,
                "agent_id": agent_id,
                "score": round(item.score, 4),
                "reason": item.reason,
                "agent_load": round(item.agent_load, 4),
            },
        )
        policy = node.retry or RetryPolicy()
        max_attempts = max(1, int(getattr(policy, "max_attempts", 1) or 1))
        last_error = ""
        last_verify: VerifyResult | None = None
        for attempt in range(1, max_attempts + 1):
            self._check_cancelled(run)
            with run.lock:
                state = run.ensure_node(node.id)
                state.status = NodeStatus.RUNNING.value
                state.attempts += 1
                state.started_at = time.time()
                state.finished_at = 0.0
                state.agent_id = agent_id
                state.error = ""
            self._emit(
                run,
                EventType.NODE_STARTED,
                node_id=node.id,
                agent_id=agent_id or None,
                payload={"node_id": node.id, "agent_id": agent_id, "attempt": attempt},
            )
            try:
                result = await asyncio.wait_for(
                    self._run_guarded(workflow, run, node, item, depth=depth),
                    timeout=float(node.timeout_s) if float(node.timeout_s or 0) > 0 else None,
                )
            except (_CancelRun, _AbortRun, asyncio.CancelledError):
                raise
            except asyncio.TimeoutError:
                last_error = f"节点执行超时（{node.timeout_s}s）"
                last_verify = None
            except NodeExecutionError as exc:
                last_error = str(exc) or "节点执行失败"
                last_verify = exc.verify
                if exc.output is not None:
                    run.set_var(node.id, exc.output)
            except Exception as exc:  # pragma: no cover - 未知异常按失败处理
                last_error = f"{type(exc).__name__}: {exc}"
                last_verify = None
            else:
                return self._finish_node(run, node, result)
            # 失败：按 RetryPolicy 决定是否重试
            if attempt < max_attempts and self._retry_wanted(policy, last_verify):
                self._emit(
                    run,
                    EventType.NODE_RETRYING,
                    node_id=node.id,
                    agent_id=agent_id or None,
                    payload={
                        "node_id": node.id,
                        "error": last_error,
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                    },
                    level="warning",
                )
                backoff = float(getattr(policy, "backoff_s", 0.0) or 0.0)
                if backoff > 0:
                    await asyncio.sleep(min(10.0, backoff * attempt))
                continue
            if last_verify is not None:
                self._store_verify(run, node, last_verify)
            run.set_node_status(node.id, NodeStatus.FAILED.value, error=last_error)
            self._emit(
                run,
                EventType.NODE_FAILED,
                node_id=node.id,
                agent_id=agent_id or None,
                payload={"node_id": node.id, "error": last_error, "attempt": attempt},
                level="error",
            )
            self._record_feedback(agent_id, False)
            strategy = self._failure_strategy(node)
            return _Outcome(ok=False, error=last_error, strategy=strategy)
        # 理论上不可达
        return _Outcome(ok=False, error=last_error or "节点执行失败", strategy="abort")

    def _finish_node(
        self, run: RunState, node: WorkflowNode, result: _NodeOutput
    ) -> _Outcome:
        """写入节点输出与状态，广播成功/失败事件。"""
        self._store_output(run, node, result.output)
        if result.verify is not None:
            self._store_verify(run, node, result.verify)
        if result.failed:
            run.set_node_status(
                node.id,
                NodeStatus.FAILED.value,
                error=result.error or "节点执行失败",
            )
            self._emit(
                run,
                EventType.NODE_FAILED,
                node_id=node.id,
                agent_id=result.agent_id or node.agent or None,
                payload={
                    "node_id": node.id,
                    "error": result.error or "节点执行失败",
                    "attempt": 1,
                },
                level="error",
            )
            self._record_feedback(result.agent_id or node.agent or "", False)
            return _Outcome(
                ok=False,
                error=result.error or "节点执行失败",
                strategy=self._failure_strategy(node),
                branch_targets=result.branch_targets,
                output=result.output,
            )
        run.set_node_status(
            node.id,
            NodeStatus.SUCCEEDED.value,
            output=result.output,
            confidence=round(float(result.confidence or 0.0), 4),
            tokens_in=int(result.tokens_in or 0),
            tokens_out=int(result.tokens_out or 0),
            agent_id=result.agent_id or node.agent,
        )
        self._emit(
            run,
            EventType.NODE_SUCCEEDED,
            node_id=node.id,
            agent_id=result.agent_id or node.agent or None,
            payload={
                "node_id": node.id,
                "output_preview": _preview(result.output),
                "confidence": round(float(result.confidence or 0.0), 4),
                "tokens_in": int(result.tokens_in or 0),
                "tokens_out": int(result.tokens_out or 0),
                "latency_s": round(float(result.latency_s or 0.0), 4),
            },
        )
        self._record_feedback(result.agent_id or node.agent or "", True)
        return _Outcome(
            ok=True,
            branch_targets=result.branch_targets,
            output=result.output,
        )

    def _record_feedback(self, agent_id: str, success: bool) -> None:
        """把节点结果反馈给调度器。

        ``adaptive`` 策略的「历史成功率」与权重自适应依赖该反馈；调度器异常
        不能影响节点执行，因此这里吞掉异常只记 debug 日志。
        """
        scheduler = getattr(self.ctx, "scheduler", None) if self.ctx is not None else None
        record = getattr(scheduler, "record_outcome", None)
        if not callable(record):
            return
        try:
            record(agent_id, bool(success))
        except Exception:  # pragma: no cover - 反馈失败不影响执行
            log.debug("调度反馈写入失败", exc_info=True)

    async def _run_guarded(
        self,
        workflow: Workflow,
        run: RunState,
        node: WorkflowNode,
        item: _SchedInfo,
        depth: int = 0,
    ) -> _NodeOutput:
        """叶子节点受 Semaphore 约束；结构型节点直接执行避免死锁。"""
        node_type = _node_type(node)
        if node_type in (NodeType.LOOP.value, NodeType.SUBWORKFLOW.value):
            return await self._execute_node(workflow, run, node, item, depth=depth)
        semaphore = self._semaphore or asyncio.Semaphore(1)
        async with semaphore:
            return await self._execute_node(workflow, run, node, item, depth=depth)

    async def _execute_node(
        self,
        workflow: Workflow,
        run: RunState,
        node: WorkflowNode,
        item: _SchedInfo,
        depth: int = 0,
    ) -> _NodeOutput:
        """按节点类型分发到具体执行器。"""
        node_type = _node_type(node)
        if node_type == NodeType.START.value:
            return self._execute_start(run)
        if node_type == NodeType.END.value:
            return self._execute_end(run)
        if node_type == NodeType.AGENT.value:
            return await self._execute_agent(workflow, run, node, item)
        if node_type == NodeType.TOOL.value:
            return await self._execute_tool(workflow, run, node)
        if node_type == NodeType.CONDITION.value:
            return self._execute_condition(workflow, run, node)
        if node_type == NodeType.LOOP.value:
            return await self._execute_loop_node(workflow, run, node, depth=depth)
        if node_type == NodeType.SUBWORKFLOW.value:
            return await self._execute_subworkflow(workflow, run, node, depth=depth)
        if node_type == NodeType.VERIFY.value:
            return await self._execute_verify_node(workflow, run, node)
        if node_type == NodeType.HUMAN_GATE.value:
            return await self._execute_human_gate(run, node)
        if node_type == NodeType.JOIN.value:
            return self._execute_join(workflow, run, node)
        raise NodeExecutionError(f"未知节点类型: {node_type}")

    # ------------------------------------------------------------------
    # 各类节点执行器
    # ------------------------------------------------------------------
    def _execute_start(self, run: RunState) -> _NodeOutput:
        """start 节点：暴露运行输入。"""
        return _NodeOutput(
            output={"inputs": dict(run.inputs), "started": True},
            confidence=1.0,
            agent_id="",
        )

    def _execute_end(self, run: RunState) -> _NodeOutput:
        """end 节点：记录结束标记。"""
        metrics = run.metrics()
        return _NodeOutput(
            output={
                "finished": True,
                "nodes_succeeded": metrics.get("nodes_succeeded", 0),
                "success_rate": metrics.get("success_rate", 0.0),
            },
            confidence=1.0,
        )

    async def _execute_agent(
        self,
        workflow: Workflow,
        run: RunState,
        node: WorkflowNode,
        item: _SchedInfo,
    ) -> _NodeOutput:
        """agent 节点：调用 Agent，必要时进入「验证—反馈—重跑」自循环。"""
        loop_spec = node.loop or LoopSpec()
        feedback_var = str(loop_spec.feedback_var or "feedback")
        # 循环体节点可能被上一轮反馈驱动：把当前反馈透传给 AgentRuntime，
        # 使 feedback 变量在 prompt 模板之外也能被 Agent 直接读取。
        feedback = str(
            run.variables.get(feedback_var)
            or run.variables.get("feedback")
            or ""
        )
        spec = node.verify
        pipeline = getattr(self.ctx, "verifier", None)
        controller = SelfLoopController(loop_spec)
        state = controller.begin()
        output: Any = None
        result: Any = None
        verify: VerifyResult | None = None
        tokens_in = 0
        tokens_out = 0
        latency = 0.0
        agent_id = node.agent or item.agent_id or self._resolve_agent(node)
        prompt_template = node.prompt or node.description or node.label or node.id
        confidence = 0.0
        while True:
            self._check_cancelled(run)
            context_vars = self._context(run, workflow)
            prompt = render_template(prompt_template, context_vars)
            started = time.time()
            self._enter_agent(agent_id)
            try:
                node_state = run.ensure_node(node.id)
                result = await self.ctx.call_agent(
                    agent_id,
                    prompt,
                    run,
                    node,
                    context=self._agent_context(run, workflow, node, feedback),
                    feedback=feedback,
                    node_state=node_state,
                )
            finally:
                self._leave_agent(agent_id)
            latency += time.time() - started
            tokens_in += int(getattr(result, "tokens_in", 0) or 0)
            tokens_out += int(getattr(result, "tokens_out", 0) or 0)
            if getattr(result, "error", None):
                raise NodeExecutionError(str(result.error))
            output = getattr(result, "output", result)
            confidence = float(getattr(result, "confidence", 0.0) or 0.0)
            if spec is None or pipeline is None or not hasattr(pipeline, "verify"):
                state.converged = True
                state.stop_reason = "converged"
                break
            verify = await pipeline.verify(spec, node, output, run)
            self._store_verify(run, node, verify)
            if verify.passed:
                controller.record(state, verify, output)
                state.converged = True
                state.stop_reason = "converged"
                break
            if not spec.allow_self_repair:
                raise NodeExecutionError(
                    verify.feedback or "验证未通过",
                    verify=verify,
                    output=output,
                )
            controller.record(state, verify, output)
            if not controller.should_continue(verify, state):
                break
            feedback = controller.build_feedback(verify, state)
            run.set_var(loop_spec.feedback_var, feedback)
            run.set_var("feedback", feedback)
            self._emit(
                run,
                EventType.LOOP_ITERATION,
                node_id=node.id,
                agent_id=agent_id or None,
                payload={
                    "node_id": node.id,
                    "iteration": state.iteration,
                    "max_iterations": int(loop_spec.max_iterations),
                    "verify_passed": False,
                    "confidence": round(float(verify.confidence), 4),
                    "feedback_preview": feedback[:240],
                    "failures": [
                        check.name
                        for check in verify.checks
                        if not check.passed and not check.evidence.get("skipped")
                    ],
                },
            )
        summary = controller.summary(state)
        summary["passed"] = bool(verify.passed) if verify is not None else True
        run.set_self_loop(node.id, summary)
        if verify is not None and not verify.passed:
            raise NodeExecutionError(
                verify.feedback or "验证未通过",
                verify=verify,
                output=output,
            )
        if verify is not None:
            confidence = float(verify.confidence)
        return _NodeOutput(
            output=output,
            confidence=confidence,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_s=latency,
            agent_id=agent_id,
            verify=verify,
        )

    async def _execute_tool(
        self, workflow: Workflow, run: RunState, node: WorkflowNode
    ) -> _NodeOutput:
        """tool 节点：渲染参数并调用工具。"""
        tool_name = node.tool or str(node.config.get("tool") or "")
        if not tool_name:
            raise NodeExecutionError(f"tool 节点 {node.id} 未配置工具名")
        context_vars = self._context(run, workflow)
        args = _render_value(node.args or {}, context_vars)
        self._emit(
            run,
            EventType.TOOL_CALLED,
            node_id=node.id,
            agent_id=node.agent or None,
            payload={"name": tool_name, "args": args},
        )
        started = time.time()
        result = await self.ctx.call_tool(tool_name, args)
        duration = float(getattr(result, "duration_s", 0.0) or (time.time() - started))
        ok = bool(getattr(result, "ok", False))
        value = getattr(result, "value", None)
        self._emit(
            run,
            EventType.TOOL_RESULT,
            node_id=node.id,
            agent_id=node.agent or None,
            payload={
                "name": tool_name,
                "ok": ok,
                "duration_s": round(duration, 4),
                "preview": _preview(value),
            },
            level="info" if ok else "error",
        )
        if not ok:
            raise NodeExecutionError(
                str(getattr(result, "error", "") or f"工具 {tool_name} 调用失败")
            )
        verify = await self._verify_optional(node, value, run)
        if verify is not None and not verify.passed:
            raise NodeExecutionError(
                verify.feedback or "输出验证未通过",
                verify=verify,
                output=value,
            )
        return _NodeOutput(
            output=value,
            confidence=float(verify.confidence) if verify is not None else 0.85,
            latency_s=duration,
            agent_id=node.agent,
            verify=verify,
        )

    def _execute_condition(
        self, workflow: Workflow, run: RunState, node: WorkflowNode
    ) -> _NodeOutput:
        """condition 节点：求值出边条件，选择分支。"""
        context_vars = self._context(run, workflow)
        edges = [
            edge
            for edge in workflow.edges
            if edge.source == node.id and edge.kind != "loop_back"
        ]
        guard_ok = True
        if node.condition:
            try:
                guard_ok = truthy(node.condition, context_vars)
            except ExprError as exc:
                raise NodeExecutionError(f"条件表达式求值失败: {exc}") from exc
        chosen: list[WorkflowEdge] = []
        matched = ""
        if guard_ok:
            for edge in edges:
                if not edge.condition:
                    continue
                try:
                    if truthy(edge.condition, context_vars):
                        chosen = [edge]
                        matched = edge.condition
                        break
                except ExprError as exc:
                    raise NodeExecutionError(
                        f"分支条件求值失败（{edge.condition}）: {exc}"
                    ) from exc
            if not chosen:
                chosen = [edge for edge in edges if not edge.condition]
        targets = [edge.target for edge in chosen]
        output = {
            "branch": targets[0] if len(targets) == 1 else targets,
            "targets": targets,
            "matched_condition": matched,
            "guard": node.condition,
        }
        return _NodeOutput(
            output=output,
            confidence=1.0,
            branch_targets=targets,
        )

    async def _execute_loop_node(
        self,
        workflow: Workflow,
        run: RunState,
        node: WorkflowNode,
        depth: int = 0,
    ) -> _NodeOutput:
        """loop 节点：迭代执行循环体，直到验证通过 / 达到上限 / 表达式退出。"""
        spec = node.loop or LoopSpec()
        body = self._loop_body(workflow, node)
        if not body:
            return _NodeOutput(
                output={"iterations": 0, "passed": True, "note": "循环体为空"},
                confidence=1.0,
            )
        max_iterations = max(1, int(spec.max_iterations or 1))
        controller = SelfLoopController(spec)
        state = controller.begin()
        feedback = ""
        verify: VerifyResult | None = None
        passed = False
        failures: list[str] = []
        exit_when = False
        for iteration in range(1, max_iterations + 1):
            self._check_cancelled(run)
            if feedback:
                run.set_var(spec.feedback_var, feedback)
                run.set_var("feedback", feedback)
            verification_before = len(run.verification)
            await self._execute_region(workflow, run, body, depth=depth + 1)
            verify = self._verify_from_records(run, verification_before)
            exit_when = self._exit_when_matched(spec, run, workflow)
            if verify is None:
                # 循环体没有验证节点：单轮执行即视为收敛
                state.converged = True
                state.stop_reason = "converged"
                passed = True
                self._emit_loop_iteration(run, node, spec, state, verify, passed, failures)
                break
            if verify.passed:
                controller.record(state, verify, None)
                state.converged = True
                state.stop_reason = "converged"
                passed = True
                self._emit_loop_iteration(run, node, spec, state, verify, passed, failures)
                break
            controller.record(state, verify, None)
            failures = [
                check.name
                for check in verify.checks
                if not check.passed and not check.evidence.get("skipped")
            ]
            feedback = controller.build_feedback(verify, state)
            self._emit_loop_iteration(run, node, spec, state, verify, False, failures)
            if exit_when:
                state.stop_reason = "exit_when"
                break
            if not controller.should_continue(verify, state):
                break
        summary = controller.summary(state)
        summary["passed"] = passed
        summary["stop_reason"] = state.stop_reason
        summary["failures"] = failures
        run.set_self_loop(node.id, summary)
        output = {
            "iterations": state.iteration,
            "max_iterations": max_iterations,
            "converged": passed,
            "exhausted": bool(state.exhausted),
            "passed": passed,
            "stop_reason": state.stop_reason,
            "confidence": round(float(verify.confidence), 4) if verify else 0.0,
            "failures": failures,
            "history": summary.get("history", []),
        }
        self._emit(
            run,
            EventType.LOOP_EXIT,
            node_id=node.id,
            payload={
                "node_id": node.id,
                "iterations": state.iteration,
                "converged": passed,
                "exhausted": bool(state.exhausted),
                "stop_reason": state.stop_reason,
                "confidence": output["confidence"],
            },
        )
        if not passed:
            return _NodeOutput(
                output=output,
                verify=verify,
                failed=True,
                error=f"循环未收敛（{state.stop_reason}，{state.iteration} 轮）",
            )
        return _NodeOutput(
            output=output,
            confidence=output["confidence"],
            verify=verify,
        )

    async def _execute_subworkflow(
        self,
        workflow: Workflow,
        run: RunState,
        node: WorkflowNode,
        depth: int = 0,
    ) -> _NodeOutput:
        """subworkflow 节点：在子 RunState 中递归执行并合并结果。"""
        workflow_id = node.workflow or str(node.config.get("workflow") or "")
        if not workflow_id:
            raise NodeExecutionError(f"subworkflow 节点 {node.id} 未指定子工作流 id")
        child_wf = self._resolve_workflow(workflow_id)
        if child_wf is None:
            raise NodeExecutionError(f"子工作流不存在: {workflow_id}")
        child_inputs = _render_value(node.args or {}, self._context(run, workflow))
        child = RunState(
            run_id=run.run_id,
            workflow_id=child_wf.id,
            workflow_name=child_wf.name,
            inputs=dict(child_inputs),
            max_steps=self.max_steps,
            artifacts_dir=run.artifacts_dir,
        )
        child.variables.update(dict(child_inputs))
        child.started_at = time.time()
        child.status = RunStatus.RUNNING.value
        try:
            await self._run_graph(child_wf, child, depth=depth + 1)
        except _AbortRun as exc:
            raise NodeExecutionError(f"子工作流 {child_wf.id} 执行失败: {exc}") from exc
        failed = [
            state.node_id
            for state in child.node_states.values()
            if state.status == NodeStatus.FAILED.value
        ]
        for child_id, child_state in child.node_states.items():
            run.node_states[f"{node.id}.{child_id}"] = child_state
        for key, value in child.variables.items():
            run.variables.setdefault(f"{node.id}.{key}", value)
        for artifact in child.artifacts:
            if isinstance(artifact, dict):
                run.add_artifact(
                    str(artifact.get("name") or "artifact"),
                    str(artifact.get("path") or ""),
                    str(artifact.get("kind") or "file"),
                )
        output = {
            "workflow": child_wf.id,
            "status": RunStatus.FAILED.value if failed else RunStatus.SUCCEEDED.value,
            "failed_nodes": failed,
            "variables": dict(child.variables),
        }
        if failed:
            raise NodeExecutionError(
                f"子工作流 {child_wf.id} 存在失败节点: {', '.join(failed[:5])}",
                output=output,
            )
        return _NodeOutput(output=output, confidence=1.0)

    async def _execute_verify_node(
        self, workflow: Workflow, run: RunState, node: WorkflowNode
    ) -> _NodeOutput:
        """verify 节点：对前驱（或 config.target）输出执行验证流水线。"""
        spec = node.verify
        if spec is None:
            raise NodeExecutionError(f"verify 节点 {node.id} 缺少验证配置")
        target_output = self._verify_target_output(workflow, run, node)
        pipeline = getattr(self.ctx, "verifier", None)
        if pipeline is None or not hasattr(pipeline, "verify"):
            output = {
                "passed": False,
                "skipped": True,
                "reason": "验证流水线不可用（ctx.verifier 为空）",
            }
            return _NodeOutput(
                output=output,
                confidence=0.0,
                failed=True,
                error="验证流水线不可用",
            )
        verify = await pipeline.verify(spec, node, target_output, run)
        self._store_verify(run, node, verify)
        run.set_var(node.id, verify.to_dict())
        approved = True
        if spec.require_human:
            approved = await self.ctx.human_gate(node, run)
        failed = (not verify.passed) or (not approved)
        output = verify.to_dict()
        output["target"] = str(node.config.get("target") or "")
        output["human_approved"] = approved
        error = ""
        if not verify.passed:
            error = verify.feedback or "验证未通过"
        if not approved:
            error = (error + "；人工复核未通过").strip("；")
        return _NodeOutput(
            output=output,
            confidence=float(verify.confidence),
            verify=verify,
            failed=failed,
            error=error,
        )

    async def _execute_human_gate(
        self, run: RunState, node: WorkflowNode
    ) -> _NodeOutput:
        """human_gate 节点：调用人工审核回调。"""
        approved = await self.ctx.human_gate(node, run)
        output = {"approved": bool(approved), "node_id": node.id}
        if not approved:
            return _NodeOutput(
                output=output,
                failed=True,
                error="人工审核未通过",
                agent_id="human",
            )
        return _NodeOutput(output=output, confidence=1.0, agent_id="human")

    def _execute_join(
        self, workflow: Workflow, run: RunState, node: WorkflowNode
    ) -> _NodeOutput:
        """join 节点：汇聚前驱输出。"""
        predecessors = [
            edge.source
            for edge in workflow.edges
            if edge.target == node.id and edge.kind != "loop_back"
        ]
        joined = {
            predecessor: run.variables.get(predecessor) for predecessor in predecessors
        }
        output = {
            "joined": joined,
            "count": len(joined),
            "value": joined.get(predecessors[0]) if len(predecessors) == 1 else joined,
        }
        return _NodeOutput(output=output, confidence=1.0)

    # ------------------------------------------------------------------
    # 验证辅助
    # ------------------------------------------------------------------
    async def _verify_optional(
        self, node: WorkflowNode, output: Any, run: RunState
    ) -> VerifyResult | None:
        """对非 agent 节点做一次验证（有配置才执行）。"""
        spec = node.verify
        pipeline = getattr(self.ctx, "verifier", None)
        if spec is None or pipeline is None or not hasattr(pipeline, "verify"):
            return None
        verify = await pipeline.verify(spec, node, output, run)
        self._store_verify(run, node, verify)
        return verify

    def _store_verify(self, run: RunState, node: WorkflowNode, verify: VerifyResult) -> None:
        """把验证结果写到节点状态（供前端与报告读取）。"""
        state = run.ensure_node(node.id)
        with run.lock:
            state.verify = verify.to_dict()
            state.confidence = round(float(verify.confidence), 4)

    def _verify_target_output(
        self, workflow: Workflow, run: RunState, node: WorkflowNode
    ) -> Any:
        """推断 verify 节点要校验的对象输出。"""
        explicit = str(node.config.get("target") or "")
        if explicit:
            if explicit in run.variables:
                return run.variables[explicit]
            state = run.node_states.get(explicit)
            return state.output if state is not None else None
        for edge in workflow.edges:
            if edge.target != node.id or edge.kind == "loop_back":
                continue
            if edge.source in run.variables:
                return run.variables[edge.source]
            state = run.node_states.get(edge.source)
            if state is not None and state.output is not None:
                return state.output
        return run.variables.get("output")

    def _verify_from_records(
        self, run: RunState, before: int
    ) -> VerifyResult | None:
        """从新增的验证记录里取最后一个还原为 ``VerifyResult``。"""
        records = run.verification[before:]
        for record in reversed(records):
            if isinstance(record, dict) and "passed" in record:
                return _record_to_verify(record)
        return None

    def _exit_when_matched(
        self, spec: LoopSpec, run: RunState, workflow: Workflow
    ) -> bool:
        """求值循环退出表达式（空表达式视为不退出）。"""
        if not spec.exit_when:
            return False
        try:
            return truthy(spec.exit_when, self._context(run, workflow))
        except ExprError:
            log.debug("循环退出表达式求值失败: %s", spec.exit_when, exc_info=True)
            return False

    def _emit_loop_iteration(
        self,
        run: RunState,
        node: WorkflowNode,
        spec: LoopSpec,
        state: Any,
        verify: VerifyResult | None,
        passed: bool,
        failures: list[str],
    ) -> None:
        """广播 ``loop.iteration`` 事件（含反馈预览与失败检查项）。"""
        feedback = state.feedback if isinstance(getattr(state, "feedback", None), str) else ""
        if passed:
            # 已经通过的一轮不应再携带上一轮的反馈与失败项，否则前端/事件回放
            # 会把“第 2 轮通过”误读成“仍在失败”。失败项以本轮验证为准。
            feedback = ""
            failures = []
        self._emit(
            run,
            EventType.LOOP_ITERATION,
            node_id=node.id,
            payload={
                "node_id": node.id,
                "iteration": state.iteration,
                "max_iterations": int(spec.max_iterations),
                "verify_passed": bool(passed),
                "confidence": round(float(verify.confidence), 4) if verify else 0.0,
                "feedback_preview": feedback[:240],
                "failures": list(failures),
            },
        )

    # ------------------------------------------------------------------
    # 上下文 / 工具函数
    # ------------------------------------------------------------------
    def _context(self, run: RunState, workflow: Workflow) -> dict[str, Any]:
        """构造模板渲染/条件求值用的扁平上下文。"""
        context: dict[str, Any] = dict(workflow.variables or {})
        context.update(run.inputs)
        context.update(run.variables)
        context.setdefault("input", dict(run.inputs))
        context.setdefault("workflow_id", workflow.id)
        context.setdefault("run_id", run.run_id)
        context.setdefault("feedback", run.variables.get("feedback", ""))
        context["nodes"] = {
            node_id: state.output for node_id, state in run.node_states.items()
        }
        return context

    def _agent_context(
        self,
        run: RunState,
        workflow: Workflow,
        node: WorkflowNode,
        feedback: str = "",
    ) -> dict[str, Any]:
        """传给 Agent 的结构化上下文（变量 + 节点输出 + 反馈）。

        注意：**不写入空的 feedback 键**——空的 ``"feedback": ""`` 除了制造噪声，
        还会让按关键字判断「修复轮」的离线 Mock 误以为已经收到验证反馈。
        只有非空反馈才同时出现在 ``variables`` 与顶层 ``feedback`` 字段中。
        """
        variables = dict(run.variables)
        if not str(variables.get("feedback", "") or "").strip():
            variables.pop("feedback", None)
        context: dict[str, Any] = {
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "run_id": run.run_id,
            "node_id": node.id,
            "node_label": node.label,
            "inputs": dict(run.inputs),
            "variables": variables,
            "node_outputs": {
                node_id: state.output for node_id, state in run.node_states.items()
            },
        }
        if str(feedback or "").strip():
            context["feedback"] = feedback
        return context

    def _store_output(self, run: RunState, node: WorkflowNode, output: Any) -> None:
        """把节点输出写入运行变量，并展开声明的 outputs 字段。"""
        run.set_var(node.id, output)
        run.set_var("output", output)
        if isinstance(output, dict):
            for key in node.outputs or []:
                if key in output:
                    run.set_var(str(key), output[key])

    def _resolve_agent(self, node: WorkflowNode) -> str:
        """解析节点应当使用的 Agent id（容忍无 Agent 注册表）。"""
        if node.agent:
            return node.agent
        config_agent = str(node.config.get("agent") or "")
        if config_agent:
            return config_agent
        agents = getattr(self.ctx, "agents", None)
        skills = node.config.get("skills") or []
        if agents is not None and hasattr(agents, "match"):
            try:
                matches = agents.match(list(skills) or None, None)
            except Exception:  # pragma: no cover - 注册表异常不阻断
                matches = []
            for spec_item, _score in matches or []:
                agent_id = str(getattr(spec_item, "id", "") or "")
                if agent_id:
                    return agent_id
        return ""

    def _capability(self, node: WorkflowNode, agent_id: str) -> float:
        """估算节点—Agent 能力匹配度（0~1）。"""
        if not agent_id:
            return 0.5
        agents = getattr(self.ctx, "agents", None)
        if agents is None or not hasattr(agents, "match"):
            return 0.8
        skills = node.config.get("skills") or []
        try:
            matches = agents.match(list(skills) or None, None)
        except Exception:  # pragma: no cover
            return 0.8
        for spec_item, score in matches or []:
            if str(getattr(spec_item, "id", "")) == agent_id:
                return max(0.0, min(1.0, float(score)))
        return 0.7

    def _resolve_workflow(self, workflow_id: str) -> Workflow | None:
        """从上下文的工作流仓库里取子工作流（兼容 dict / Store 两种形态）。"""
        workflows = getattr(self.ctx, "workflows", None)
        if workflows is None:
            return None
        if isinstance(workflows, dict):
            return workflows.get(workflow_id)
        getter = getattr(workflows, "get", None)
        if callable(getter):
            try:
                found = getter(workflow_id)
            except TypeError:  # pragma: no cover
                found = None
            if isinstance(found, Workflow):
                return found
        all_getter = getattr(workflows, "all", None)
        if callable(all_getter):
            try:
                for item in all_getter():
                    if getattr(item, "id", "") == workflow_id:
                        return item
            except Exception:  # pragma: no cover
                return None
        return None

    # ------------------------------------------------------------------
    # 调度辅助
    # ------------------------------------------------------------------
    def _enter_agent(self, agent_id: str) -> None:
        if agent_id:
            self._agent_loads[agent_id] = self._agent_loads.get(agent_id, 0) + 1

    def _leave_agent(self, agent_id: str) -> None:
        if not agent_id:
            return
        current = self._agent_loads.get(agent_id, 0) - 1
        if current <= 0:
            self._agent_loads.pop(agent_id, None)
        else:
            self._agent_loads[agent_id] = current

    @staticmethod
    def _retry_wanted(policy: RetryPolicy, verify: VerifyResult | None) -> bool:
        """按 RetryPolicy.on 判断本次失败是否值得重试。"""
        on = set(getattr(policy, "on", None) or ["error"])
        if verify is not None:
            return "verify_failed" in on
        return "error" in on

    @staticmethod
    def _failure_strategy(node: WorkflowNode) -> str:
        """读取节点的失败策略：abort / continue / skip。"""
        strategy = str(node.config.get("on_failure") or "").strip().lower()
        if strategy not in _OUT_STRATEGIES:
            strategy = "continue" if _node_type(node) == NodeType.VERIFY.value else "abort"
        return strategy

    # ------------------------------------------------------------------
    # 取消 / 事件
    # ------------------------------------------------------------------
    def _check_cancelled(self, run: RunState) -> None:
        """检查取消标志；命中则抛出内部取消信号。"""
        if self._is_cancelled(run):
            raise _CancelRun()

    def _is_cancelled(self, run: RunState) -> bool:
        """综合运行状态与上下文取消标志判断是否已取消。"""
        try:
            if run.is_cancelled():
                return True
        except Exception:  # pragma: no cover
            pass
        event = getattr(self.ctx, "cancel_event", None)
        if event is not None and getattr(event, "is_set", None) and event.is_set():
            return True
        checker = getattr(self.ctx, "is_cancelled", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # pragma: no cover
                return False
        return False

    def _emit(
        self,
        run: RunState,
        event_type: str,
        node_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        """安全广播事件：事件系统异常不允许中断执行。"""
        emit = getattr(self.ctx, "emit", None)
        if not callable(emit):
            return
        try:
            emit(
                event_type,
                run_id=run.run_id,
                node_id=node_id,
                agent_id=agent_id,
                payload=payload or {},
                level=level,
            )
        except Exception:  # pragma: no cover - 事件失败不影响执行
            log.debug("事件广播失败: %s", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------
def _safe_float(value: Any, default: float) -> float:
    """宽松地转 float（失败返回默认值）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    """递归渲染 dict/list/str 中的 ``{{var}}`` 模板。"""
    if isinstance(value, dict):
        return {key: _render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    if isinstance(value, str):
        try:
            return render_template(value, context, keep_missing=True)
        except ExprError:  # pragma: no cover - 渲染失败保留原文
            return value
    return value


def _record_to_verify(record: dict[str, Any]) -> VerifyResult:
    """把 ``run.verification`` 中的记录还原成 ``VerifyResult``。"""
    checks: list[VerifyCheckResult] = []
    for item in record.get("checks") or []:
        if not isinstance(item, dict):
            continue
        checks.append(
            VerifyCheckResult(
                name=str(item.get("name") or "check"),
                passed=bool(item.get("passed")),
                detail=str(item.get("detail") or ""),
                weight=float(item.get("weight", 1.0) or 1.0),
                evidence=dict(item.get("evidence") or {}),
            )
        )
    return VerifyResult(
        passed=bool(record.get("passed")),
        confidence=float(record.get("confidence", 0.0) or 0.0),
        checks=checks,
        feedback=str(record.get("feedback") or ""),
        metrics=dict(record.get("metrics") or {}),
        layer=str(record.get("layer") or "auto"),
    )


__all__ = [
    "EngineResult",
    "NodeExecutionError",
    "WorkflowEngine",
    "MAX_SUBWORKFLOW_DEPTH",
]
