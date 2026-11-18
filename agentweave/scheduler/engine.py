"""可解释任务调度器（五种策略 + round_robin）。

为什么需要调度器
----------------
多 Agent 系统中同一时刻可能有多个节点就绪、多个 Agent 可选。调度器负责给出
**可解释**的选择：不仅返回「选谁」，还返回 ``score`` 与 ``reason``，前端据此
展示「为什么由它执行」。打分统一采用加权和，权重在 ``describe()`` 中公开。

自适应策略（adaptive）公式::

    score = w_cap * capability + w_load * (1 - agent_load)
          + w_pri * priority_norm + w_succ * agent_success_rate

默认权重 ``w = (0.40, 0.30, 0.20, 0.10)``；运行期若整体成功率偏低则提高
``w_cap``（能力优先），若平均负载偏高则提高 ``w_load``（避免排队），并归一化。

纯标准库实现；策略初始顺序固定，保证同一候选列表重复调用结果确定。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.types import WorkflowNode


@dataclass
class ScheduleCandidate:
    """一个待调度候选：节点 + 候选 Agent 的能力/负载画像。"""

    node: WorkflowNode
    priority: float = 0.0            # 数值越大越优先
    agent_id: str = ""
    agent_load: float = 0.0          # 0~1，越大越忙
    capability: float = 0.0          # 0~1，能力匹配度
    est_cost: float = 0.0
    data_ready: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node.id,
            "priority": round(self.priority, 4),
            "agent_id": self.agent_id,
            "agent_load": round(self.agent_load, 4),
            "capability": round(self.capability, 4),
            "est_cost": round(self.est_cost, 4),
            "data_ready": self.data_ready,
        }


@dataclass
class ScheduleDecision:
    """调度决策结果（可解释）。"""

    node_id: str
    agent_id: str
    score: float
    reason: str
    agent_load: float = 0.0
    candidate_list: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "score": round(self.score, 4),
            "reason": self.reason,
            "agent_load": round(self.agent_load, 4),
            "candidate_list": list(self.candidate_list),
        }


#: 策略显示顺序（含契约中的 priority 与任务书中的 round_robin，二者语义不同）
POLICY_ORDER: list[str] = [
    "fifo",
    "priority",
    "round_robin",
    "capability",
    "load_aware",
    "adaptive",
]

_POLICY_DOCS: dict[str, str] = {
    "fifo": "先来先服务：按候选到达顺序选择，score=1.0",
    "priority": "优先级最高者胜出：score=priority（归一化后展示）",
    "round_robin": "轮询：在候选间轮流选择，保证公平",
    "capability": "能力匹配最高者胜出：score=capability",
    "load_aware": "负载感知：score=0.6*(1-load)+0.4*capability，避免忙 Agent",
    "adaptive": "自适应：能力×0.4 + 空闲×0.3 + 优先级×0.2 + 历史成功率×0.1，权重随运行态调整",
}

#: 策略别名 → 规范名（兼容 ``round-robin`` / ``rr`` / ``load`` 等写法）
POLICY_ALIASES: dict[str, str] = {
    "roundrobin": "round_robin",
    "rr": "round_robin",
    "load": "load_aware",
}


def normalize_policy(policy: str) -> str:
    """把任意写法归一化为 ``POLICY_ORDER`` 中的规范策略名。

    非法策略抛 ``ValueError``，统一供 API 层（400）与平台层（领域校验）复用。
    """
    name = str(policy or "").strip().lower().replace("-", "_")
    name = POLICY_ALIASES.get(name, name)
    if name not in POLICY_ORDER:
        raise ValueError(
            f"未知调度策略: {policy!r}，可选 {', '.join(POLICY_ORDER)}"
        )
    return name


class _Stats:
    """运行态反馈统计。

    ``fork`` 出的调度器共享同一实例：策略各自独立，但 Agent 历史成功率、
    全局成功率与决策次数跨运行累积，dashboard 能看到真实学习效果。
    """

    __slots__ = ("agent", "global_success", "global_total", "decisions", "lock")

    def __init__(self) -> None:
        self.agent: dict[str, list[int]] = {}  # agent_id -> [success, total]
        self.global_success = 0
        self.global_total = 0
        self.decisions = 0
        self.lock = threading.RLock()


class Scheduler:
    """多策略调度器；``pick`` 是唯一入口。"""

    #: 策略顺序（类属性，供 describe() 与外部自省使用）
    POLICY_ORDER: list[str] = list(POLICY_ORDER)
    #: 契约中的策略名（是 ``POLICY_ORDER`` 的子集，另含 round_robin）
    POLICY_NAMES: list[str] = list(POLICY_ORDER)

    def __init__(self, policy: str = "capability") -> None:
        self._policy = "capability"
        self.set_policy(policy)
        self._rr_index = 0
        self._stats = _Stats()
        self._weights: list[float] = [0.40, 0.30, 0.20, 0.10]  # cap/load/pri/succ
        self._outcome_callback: Callable[[str, bool], None] | None = None

    # ------------------------------------------------------------------
    # 策略配置
    # ------------------------------------------------------------------
    def set_policy(self, policy: str) -> None:
        """切换策略；支持 ``round-robin`` 等连字符写法。"""
        self._policy = normalize_policy(policy)

    def fork(self, policy: str) -> "Scheduler":
        """派生一个指定策略的调度器，共享历史统计与运行期反馈。

        用于「每次运行独立生效的 per-run 策略」：运行之间互不串策略，又共享
        已经学到的 Agent 成功率与自适应权重，避免每次从零开始。
        """
        clone = Scheduler(policy)
        with self._stats.lock:
            clone._stats = self._stats
            clone._weights = list(self._weights)
            clone._outcome_callback = self._outcome_callback
        return clone

    @property
    def policy(self) -> str:
        return self._policy

    def describe(self) -> dict[str, Any]:
        """当前策略说明（供前端展示公式与权重）。"""
        return {
            "policy": self._policy,
            "policies": list(self.POLICY_ORDER),
            "description": _POLICY_DOCS.get(self._policy, ""),
            "docs": dict(_POLICY_DOCS),
            "weights": {
                "capability": round(self._weights[0], 4),
                "idle": round(self._weights[1], 4),
                "priority": round(self._weights[2], 4),
                "success": round(self._weights[3], 4),
            },
            "formula": (
                "score = w_cap*capability + w_load*(1-load) "
                "+ w_pri*priority + w_succ*agent_success_rate"
            ),
            "agent_success": {
                agent: round(ok / total, 4) if total else 0.5
                for agent, (ok, total) in self._stats.agent.items()
            },
            "global_success_rate": (
                round(self._stats.global_success / self._stats.global_total, 4)
                if self._stats.global_total
                else 0.5
            ),
            "decisions": self._stats.decisions,
        }

    # ------------------------------------------------------------------
    # 运行态反馈（自适应策略依赖）
    # ------------------------------------------------------------------
    def record_outcome(self, agent_id: str, success: bool) -> None:
        """记录一次实际执行结果，用于历史成功率与权重自适应。"""
        if not agent_id:
            return
        with self._stats.lock:
            stats = self._stats.agent.setdefault(agent_id, [0, 0])
            stats[1] += 1
            stats[0] += 1 if success else 0
            self._stats.global_total += 1
            self._stats.global_success += 1 if success else 0
            self._adapt_weights()
            callback = self._outcome_callback
        if callback is not None:
            callback(agent_id, bool(success))

    def set_outcome_callback(self, callback: Callable[[str, bool], None] | None) -> None:
        """注册外部观察者（例如落库统计）。"""
        self._outcome_callback = callback

    def _adapt_weights(self) -> None:
        """根据整体成功率与负载反馈微调权重（保持权重和为 1）。"""
        cap, load, pri, succ = self._weights
        global_rate = (
            self._stats.global_success / self._stats.global_total
            if self._stats.global_total
            else 1.0
        )
        if self._stats.global_total >= 3 and global_rate < 0.6:
            cap += 0.05
            load -= 0.05
        elif self._stats.global_total >= 3 and global_rate >= 0.9:
            succ += 0.02
            pri -= 0.02
        cap = min(max(cap, 0.10), 0.70)
        load = min(max(load, 0.10), 0.60)
        pri = min(max(pri, 0.05), 0.40)
        succ = min(max(succ, 0.05), 0.30)
        total = cap + load + pri + succ
        self._weights = [cap / total, load / total, pri / total, succ / total]

    # ------------------------------------------------------------------
    # 选择
    # ------------------------------------------------------------------
    def pick(
        self,
        candidates: list[ScheduleCandidate],
        now: float | None = None,
    ) -> ScheduleDecision | None:
        """从候选列表中选择一个并返回可解释决策；无候选返回 ``None``。"""
        _ = now  # 预留：可基于时间窗做退避，不影响当前打分
        pool = [c for c in candidates if c.data_ready]
        if not pool:
            return None
        scored = [(self._score(c, pool), c) for c in pool]
        reasons = {id(c): self._reason(c, pool) for _, c in scored}
        if self._policy == "fifo":
            chosen = scored[0][1]
        elif self._policy == "round_robin":
            chosen = pool[self._rr_index % len(pool)]
            self._rr_index = (self._rr_index + 1) % max(1, len(pool))
        else:
            # 稳定排序：同分时保留候选传入顺序，保证确定性
            best_index = 0
            best_score = scored[0][0]
            for i in range(1, len(scored)):
                if scored[i][0] > best_score:
                    best_index, best_score = i, scored[i][0]
            chosen = scored[best_index][1]
        self._stats.decisions += 1

        candidate_list = [
            {
                **c.to_dict(),
                "score": round(score, 4),
                "reason": reasons[id(c)],
            }
            for score, c in sorted(scored, key=lambda item: item[0], reverse=True)
        ]
        chosen_score, _ = next(
            ((score, c) for score, c in scored if c is chosen), (0.0, chosen)
        )
        return ScheduleDecision(
            node_id=chosen.node.id,
            agent_id=chosen.agent_id,
            score=max(0.0, min(1.0, chosen_score)),
            reason=reasons[id(chosen)],
            agent_load=chosen.agent_load,
            candidate_list=candidate_list,
        )

    # ------------------------------------------------------------------
    # 打分细节
    # ------------------------------------------------------------------
    def _score(self, c: ScheduleCandidate, pool: list[ScheduleCandidate]) -> float:
        cap = _clamp(c.capability)
        idle = 1.0 - _clamp(c.agent_load)
        max_priority = max((x.priority for x in pool), default=0.0)
        pri = _clamp(c.priority / max_priority) if max_priority > 0 else 0.0
        succ = self._success_rate(c.agent_id)
        if self._policy == "fifo":
            return 1.0
        if self._policy == "priority":
            return pri
        if self._policy == "capability":
            return cap
        if self._policy == "load_aware":
            return 0.6 * idle + 0.4 * cap
        if self._policy == "round_robin":
            return 1.0 / (1.0 + c.est_cost)
        w = self._weights
        return w[0] * cap + w[1] * idle + w[2] * pri + w[3] * succ

    def _reason(self, c: ScheduleCandidate, pool: list[ScheduleCandidate]) -> str:
        cap = _clamp(c.capability)
        idle = 1.0 - _clamp(c.agent_load)
        max_priority = max((x.priority for x in pool), default=0.0)
        pri = _clamp(c.priority / max_priority) if max_priority > 0 else 0.0
        succ = self._success_rate(c.agent_id)
        agent = c.agent_id or "未指定"
        if self._policy == "fifo":
            return f"FIFO 先来先服务：节点 {c.node.id} 到达最早，分配给 {agent}"
        if self._policy == "priority":
            return (
                f"优先级策略：节点 {c.node.id} priority={c.priority:.2f}，"
                f"归一化 {pri:.2f}，分配给 {agent}"
            )
        if self._policy == "round_robin":
            return f"轮询策略：轮到候选 {agent}，节点 {c.node.id} 依次分配"
        if self._policy == "capability":
            return (
                f"能力匹配策略：{agent} 与节点 {c.node.id} 需求匹配度 "
                f"{cap:.2f}，为候选最高"
            )
        if self._policy == "load_aware":
            return (
                f"负载感知策略：{agent} 负载 {c.agent_load:.2f}（空闲 {idle:.2f}），"
                f"能力 {cap:.2f}，综合 {0.6 * idle + 0.4 * cap:.3f}"
            )
        w = self._weights
        score = w[0] * cap + w[1] * idle + w[2] * pri + w[3] * succ
        return (
            f"自适应策略：能力 {cap:.2f}×{w[0]:.2f} + 空闲 {idle:.2f}×{w[1]:.2f}"
            f" + 优先级 {pri:.2f}×{w[2]:.2f} + 历史成功率 {succ:.2f}×{w[3]:.2f}"
            f" = {score:.3f}，分配给 {agent}"
        )

    def _success_rate(self, agent_id: str) -> float:
        stats = self._stats.agent.get(agent_id)
        if not stats or stats[1] == 0:
            return 0.5
        return stats[0] / stats[1]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """把任意数值收敛到 [low, high]，避免外部脏数据破坏打分。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


__all__ = [
    "POLICY_ALIASES",
    "POLICY_ORDER",
    "ScheduleCandidate",
    "ScheduleDecision",
    "Scheduler",
    "normalize_policy",
]
