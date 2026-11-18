"""AgentWeave 织流 —— 核心数据契约。

本模块是平台所有子系统共享的“唯一事实来源”（single source of truth）：
Agent 定义、工作流定义、运行状态、验证结果、事件对象等。

设计原则
--------
1. 纯标准库（Python 3.10+），任何模块都可以安全 import；
2. JSON 可序列化：所有对象都提供 to_dict()，工作流提供 from_dict()；
3. 不依赖具体实现：执行引擎、调度器、Web API 都只依赖这里的类型。

注意：修改本文件即修改平台契约，必须同步更新 docs/CONTRACTS.md。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class NodeType(str, Enum):
    """工作流节点类型。"""

    START = "start"                  # 入口节点
    END = "end"                      # 出口节点
    AGENT = "agent"                  # 由某个 Agent 执行的任务
    TOOL = "tool"                    # 直接调用工具的确定性任务
    CONDITION = "condition"          # 条件分支（出边带 condition 表达式）
    LOOP = "loop"                    # 循环节点（循环体子图）
    SUBWORKFLOW = "subworkflow"      # 子工作流调用
    VERIFY = "verify"                # 验证节点（调用验证流水线）
    HUMAN_GATE = "human_gate"        # 人工介入/审批节点
    JOIN = "join"                    # 汇聚节点（等待并行分支）


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"                # 等待人工审批
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class NodeStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"
    WAITING_HUMAN = "waiting_human"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# 事件类型常量（全平台统一，前端按此渲染）
# ---------------------------------------------------------------------------
class EventType:
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_FINISHED = "run.finished"
    RUN_CANCELLED = "run.cancelled"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"  # 用户/接口请求取消（终态前）
    RUN_ERROR = "run.error"                   # 编排层兜底异常（失败运行的前置事件）
    PLAN_READY = "plan.ready"                 # 调度计划生成（展示优先级/负载依据）
    NODE_SCHEDULED = "node.scheduled"         # 包含排程得分与理由
    NODE_STARTED = "node.started"
    NODE_SUCCEEDED = "node.succeeded"
    NODE_FAILED = "node.failed"
    NODE_RETRYING = "node.retrying"
    NODE_SKIPPED = "node.skipped"
    AGENT_THOUGHT = "agent.thought"           # ReAct 思考片段
    AGENT_DECISION = "agent.decision"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    LLM_CALLED = "llm.called"
    LLM_RESULT = "llm.result"
    VERIFY_RESULT = "verify.result"
    LOOP_ITERATION = "loop.iteration"
    LOOP_EXIT = "loop.exit"
    HUMAN_REQUIRED = "human.required"
    HUMAN_DECIDED = "human.decided"
    ARTIFACT = "artifact.created"
    METRIC = "metric"                         # 运行态指标采样
    LOG = "log"


# ---------------------------------------------------------------------------
# Agent / 工具定义
# ---------------------------------------------------------------------------
@dataclass
class AgentSpec:
    """Agent 角色定义（可由 UI 创建、支持版本管理）。"""

    id: str
    name: str
    role: str
    system_prompt: str
    description: str = ""
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    model: str | None = None                  # None 表示由 ModelRouter 按角色路由
    temperature: float = 0.2
    max_iterations: int = 8                   # ReAct 单任务最大迭代轮数
    memory: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "system_prompt": self.system_prompt,
            "description": self.description,
            "skills": list(self.skills),
            "tools": list(self.tools),
            "model": self.model,
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "memory": dict(self.memory),
            "version": self.version,
            "tags": list(self.tags),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSpec":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            role=str(d.get("role", "")),
            system_prompt=str(d.get("system_prompt", "")),
            description=str(d.get("description", "")),
            skills=list(d.get("skills", [])),
            tools=list(d.get("tools", [])),
            model=d.get("model"),
            temperature=float(d.get("temperature", 0.2)),
            max_iterations=int(d.get("max_iterations", 8)),
            memory=dict(d.get("memory", {})),
            version=str(d.get("version", "1.0.0")),
            tags=list(d.get("tags", [])),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class ToolSpec:
    """工具元数据（内置工具 / MCP 工具统一描述）。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)   # JSON Schema object
    source: str = "builtin"                                    # builtin | mcp | http
    server: str | None = None                                  # MCP server 名
    risk_level: str = RiskLevel.LOW.value
    timeout_s: float = 30.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "source": self.source,
            "server": self.server,
            "risk_level": self.risk_level,
            "timeout_s": self.timeout_s,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolSpec":
        return cls(
            name=str(d["name"]),
            description=str(d.get("description", "")),
            parameters=dict(d.get("parameters", {})),
            source=str(d.get("source", "builtin")),
            server=d.get("server"),
            risk_level=str(d.get("risk_level", RiskLevel.LOW.value)),
            timeout_s=float(d.get("timeout_s", 30.0)),
            tags=list(d.get("tags", [])),
        )


# ---------------------------------------------------------------------------
# 工作流定义
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    max_attempts: int = 2
    backoff_s: float = 0.5
    on: list[str] = field(default_factory=lambda: ["error"])   # error | verify_failed

    def to_dict(self) -> dict[str, Any]:
        return {"max_attempts": self.max_attempts, "backoff_s": self.backoff_s, "on": list(self.on)}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RetryPolicy":
        d = d or {}
        return cls(
            max_attempts=int(d.get("max_attempts", 2)),
            backoff_s=float(d.get("backoff_s", 0.5)),
            on=list(d.get("on", ["error"])),
        )


@dataclass
class LoopSpec:
    """循环节点 / 自循环配置。"""

    max_iterations: int = 3
    body: list[str] = field(default_factory=list)       # 循环体节点 id（无则用后继子图）
    exit_when: str | None = None                        # 表达式，为真则提前退出
    feedback_var: str = "feedback"                      # 反馈注入变量名
    mode: str = "pdca"                                  # pdca | react | simple


@dataclass
class VerifySpec:
    """节点级验证配置。"""

    checks: list[str] = field(default_factory=lambda: ["schema", "non_empty"])
    tolerance: float = 0.15              # 数值比对相对容差
    ground_truth: dict[str, Any] | None = None
    min_confidence: float = 0.7
    require_human: bool = False          # 高风险节点强制人工确认
    allow_self_repair: bool = True
    # 检查器扩展参数：如 statistical 的阈值 {n_min, r2_min}、
    # cross_agent 的 verifier_agent、code_rerun 的期望字段路径等。
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": list(self.checks),
            "tolerance": self.tolerance,
            "ground_truth": self.ground_truth,
            "min_confidence": self.min_confidence,
            "require_human": self.require_human,
            "allow_self_repair": self.allow_self_repair,
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "VerifySpec | None":
        if not d:
            return None
        return cls(
            checks=list(d.get("checks", ["schema", "non_empty"])),
            tolerance=float(d.get("tolerance", 0.15)),
            ground_truth=d.get("ground_truth"),
            min_confidence=float(d.get("min_confidence", 0.7)),
            require_human=bool(d.get("require_human", False)),
            allow_self_repair=bool(d.get("allow_self_repair", True)),
            config=dict(d.get("config", {})),
        )


@dataclass
class WorkflowEdge:
    source: str
    target: str
    condition: str | None = None        # 条件分支：表达式为真才走这条边
    label: str = ""
    kind: str = "data"                  # data | control | loop_back

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "condition": self.condition,
            "label": self.label,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowEdge":
        return cls(
            source=str(d["source"]),
            target=str(d["target"]),
            condition=d.get("condition"),
            label=str(d.get("label", "")),
            kind=str(d.get("kind", "data")),
        )


@dataclass
class WorkflowNode:
    id: str
    type: str                                   # NodeType 值
    label: str = ""
    description: str = ""
    agent: str = ""                             # type==agent 时指定 Agent id
    tool: str = ""                              # type==tool 时指定工具名
    args: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""                            # 支持 {{var}} 模板
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    condition: str = ""                         # condition 节点自身表达式（可选）
    loop: LoopSpec | None = None
    workflow: str = ""                          # type==subworkflow 时指定子工作流 id
    verify: VerifySpec | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_s: float = 120.0
    position: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "agent": self.agent,
            "tool": self.tool,
            "args": dict(self.args),
            "prompt": self.prompt,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "condition": self.condition,
            "loop": _loop_to_dict(self.loop),
            "workflow": self.workflow,
            "verify": self.verify.to_dict() if self.verify else None,
            "retry": self.retry.to_dict(),
            "timeout_s": self.timeout_s,
            "position": dict(self.position),
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowNode":
        loop = d.get("loop")
        return cls(
            id=str(d["id"]),
            type=str(d.get("type", NodeType.AGENT.value)),
            label=str(d.get("label", "")),
            description=str(d.get("description", "")),
            agent=str(d.get("agent", "")),
            tool=str(d.get("tool", "")),
            args=dict(d.get("args", {})),
            prompt=str(d.get("prompt", "")),
            inputs=list(d.get("inputs", [])),
            outputs=list(d.get("outputs", [])),
            condition=str(d.get("condition", "")),
            loop=LoopSpec(
                max_iterations=int(loop.get("max_iterations", 3)),
                body=list(loop.get("body", [])),
                exit_when=loop.get("exit_when"),
                feedback_var=str(loop.get("feedback_var", "feedback")),
                mode=str(loop.get("mode", "pdca")),
            ) if loop else None,
            workflow=str(d.get("workflow", "")),
            verify=VerifySpec.from_dict(d.get("verify")),
            retry=RetryPolicy.from_dict(d.get("retry")),
            timeout_s=float(d.get("timeout_s", 120.0)),
            position=dict(d.get("position", {"x": 0.0, "y": 0.0})),
            config=dict(d.get("config", {})),
        )


def _loop_to_dict(loop: LoopSpec | None) -> dict[str, Any] | None:
    if not loop:
        return None
    return {
        "max_iterations": loop.max_iterations,
        "body": list(loop.body),
        "exit_when": loop.exit_when,
        "feedback_var": loop.feedback_var,
        "mode": loop.mode,
    }


@dataclass
class Workflow:
    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[WorkflowEdge] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    owner: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # -- 查询辅助 ---------------------------------------------------------
    def node(self, node_id: str) -> WorkflowNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"node not found: {node_id}")

    def successors(self, node_id: str) -> list[WorkflowEdge]:
        return [e for e in self.edges if e.source == node_id]

    def predecessors(self, node_id: str) -> list[WorkflowEdge]:
        return [e for e in self.edges if e.target == node_id]

    def start_nodes(self) -> list[WorkflowNode]:
        starts = [n for n in self.nodes if n.type == NodeType.START.value]
        if starts:
            return starts
        incoming = {e.target for e in self.edges}
        return [n for n in self.nodes if n.id not in incoming]

    def end_nodes(self) -> list[WorkflowNode]:
        ends = [n for n in self.nodes if n.type == NodeType.END.value]
        if ends:
            return ends
        outgoing = {e.source for e in self.edges}
        return [n for n in self.nodes if n.id not in outgoing]

    def topological_order(self) -> list[str]:
        """返回拓扑序（忽略 loop_back 边；存在环时抛 ValueError）。"""
        ids = [n.id for n in self.nodes]
        indeg = {i: 0 for i in ids}
        adj: dict[str, list[str]] = {i: [] for i in ids}
        for e in self.edges:
            if e.kind == "loop_back":
                continue
            if e.source not in indeg or e.target not in indeg:
                continue
            adj[e.source].append(e.target)
            indeg[e.target] += 1
        queue = [i for i in ids if indeg[i] == 0]
        order: list[str] = []
        while queue:
            cur = queue.pop(0)
            order.append(cur)
            for nxt in adj[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if len(order) != len(ids):
            raise ValueError("workflow contains a cycle outside loop nodes")
        return order

    # -- 校验 -------------------------------------------------------------
    def validate(self) -> list[str]:
        """返回错误列表，空列表表示通过。"""
        errors: list[str] = []
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            errors.append("节点 id 重复")
        id_set = set(ids)
        if not id_set:
            errors.append("工作流至少需要一个节点")
        for e in self.edges:
            if e.source not in id_set:
                errors.append(f"边 {e.source}->{e.target} 的源节点不存在")
            if e.target not in id_set:
                errors.append(f"边 {e.source}->{e.target} 的目标节点不存在")
        for n in self.nodes:
            if n.type == NodeType.AGENT.value and not n.agent:
                errors.append(f"Agent 节点 {n.id} 未指定 agent")
            if n.type == NodeType.TOOL.value and not n.tool:
                errors.append(f"工具节点 {n.id} 未指定 tool")
            if n.type == NodeType.SUBWORKFLOW.value and not n.workflow:
                errors.append(f"子工作流节点 {n.id} 未指定 workflow")
            if n.type == NodeType.LOOP.value and not (n.loop and n.loop.max_iterations > 0):
                errors.append(f"循环节点 {n.id} 缺少有效循环配置")
        if not errors:
            try:
                self.topological_order()
            except ValueError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - 防御
                errors.append(f"拓扑排序失败: {exc}")
        return errors

    # -- 序列化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "variables": dict(self.variables),
            "owner": self.owner,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Workflow":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            version=str(d.get("version", "1.0.0")),
            description=str(d.get("description", "")),
            nodes=[WorkflowNode.from_dict(x) for x in d.get("nodes", [])],
            edges=[WorkflowEdge.from_dict(x) for x in d.get("edges", [])],
            variables=dict(d.get("variables", {})),
            owner=str(d.get("owner", "")),
            tags=list(d.get("tags", [])),
            created_at=float(d.get("created_at", time.time())),
        )


# ---------------------------------------------------------------------------
# 运行期请求 / 结果
# ---------------------------------------------------------------------------
@dataclass
class RunRequest:
    workflow_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    team: list[str] = field(default_factory=list)       # 参与 Agent id，空=全部可用
    policy: str = "capability"                            # 调度策略
    max_steps: int = 500
    label: str = ""
    scenario: str = ""                                    # 内置场景 id（demo）
    auto_approve: bool = True                             # 人工节点自动通过（演示模式）


@dataclass
class ToolCallRecord:
    call_id: str
    name: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None
    duration_s: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "args": self.args,
            "result": _truncate(self.result),
            "error": self.error,
            "duration_s": round(self.duration_s, 4),
            "ts": self.ts,
        }


def _truncate(value: Any, limit: int = 4000) -> Any:
    """事件/接口中的大对象截断，避免 SSE 与前端卡死。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[truncated {len(value) - limit} chars]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_truncate(v, limit // 2) for v in value[:60]]
    if isinstance(value, dict):
        return {k: _truncate(v, limit // 2) for k, v in list(value.items())[:60]}
    return str(value)[:limit]


@dataclass
class AgentResult:
    output: Any
    thoughts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float = 0.0
    confidence: float = 0.0
    iterations: int = 0
    error: str | None = None
    model: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": _truncate(self.output),
            "thoughts": self.thoughts[:40],
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_s": round(self.latency_s, 4),
            "confidence": round(self.confidence, 3),
            "iterations": self.iterations,
            "error": self.error,
            "model": self.model,
            "artifacts": dict(self.artifacts),
        }


@dataclass
class VerifyCheckResult:
    name: str
    passed: bool
    detail: str = ""
    weight: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "weight": self.weight,
            "evidence": self.evidence,
        }


@dataclass
class VerifyResult:
    passed: bool
    confidence: float
    checks: list[VerifyCheckResult] = field(default_factory=list)
    feedback: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    layer: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "confidence": round(self.confidence, 3),
            "checks": [c.to_dict() for c in self.checks],
            "feedback": self.feedback,
            "metrics": self.metrics,
            "layer": self.layer,
        }


@dataclass
class NodeState:
    node_id: str
    status: str = NodeStatus.PENDING.value
    attempts: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    agent_id: str = ""
    output: Any = None
    error: str = ""
    verify: dict[str, Any] | None = None
    confidence: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "agent_id": self.agent_id,
            "output": _truncate(self.output),
            "error": self.error,
            "verify": self.verify,
            "confidence": round(self.confidence, 3),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


@dataclass
class RunSummary:
    run_id: str
    workflow_id: str
    workflow_name: str = ""
    label: str = ""
    status: str = RunStatus.PENDING.value
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    policy: str = "capability"
    nodes: dict[str, NodeState] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    self_loop: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "policy": self.policy,
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "variables": _truncate(self.variables),
            "inputs": dict(self.inputs),
            "error": self.error,
            "metrics": self.metrics,
            "verification": self.verification,
            "self_loop": self.self_loop,
        }


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
@dataclass
class Event:
    """运行事件。SSE 推送与持久化轨迹统一使用该结构。"""

    type: str
    run_id: str = ""
    node_id: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    level: str = "info"
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "type": self.type,
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "payload": self.payload,
            "level": self.level,
        }


def new_id(prefix: str = "") -> str:
    """生成短 id：run-x8f3a2c1 之类。"""
    raw = uuid.uuid4().hex[:10]
    return f"{prefix}{raw}" if prefix else raw
