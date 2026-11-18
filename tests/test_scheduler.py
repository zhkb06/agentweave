"""调度器单元测试。

覆盖五种契约策略与 round_robin 超集：顺序、优先级、能力、负载感知、
自适应可解释公式、运行态权重自适应，以及非法策略 / 空候选等边界。
"""

from __future__ import annotations

import unittest

from agentweave.core.types import WorkflowNode
from agentweave.scheduler import POLICY_ORDER, ScheduleCandidate, Scheduler, normalize_policy


def make_node(node_id: str) -> WorkflowNode:
    """构造仅用于调度的最小节点。"""
    return WorkflowNode(id=node_id, type="agent")


def make_candidate(
    node_id: str,
    *,
    priority: float = 0.0,
    agent_id: str = "",
    agent_load: float = 0.0,
    capability: float = 0.0,
    est_cost: float = 0.0,
    data_ready: bool = True,
) -> ScheduleCandidate:
    """构造调度候选。"""
    return ScheduleCandidate(
        node=make_node(node_id),
        priority=priority,
        agent_id=agent_id,
        agent_load=agent_load,
        capability=capability,
        est_cost=est_cost,
        data_ready=data_ready,
    )


class SchedulerPolicyTest(unittest.TestCase):
    """策略选择与打分行为。"""

    def test_policy_names_cover_contract(self) -> None:
        """契约要求的 5 种策略必须全部可用，且非法策略抛 ValueError。"""
        for name in ("fifo", "priority", "capability", "load_aware", "adaptive"):
            self.assertIn(name, Scheduler.POLICY_NAMES)
            self.assertIn(name, POLICY_ORDER)
            scheduler = Scheduler(policy=name)
            self.assertEqual(scheduler.policy, name)
        with self.assertRaises(ValueError):
            Scheduler(policy="no-such-policy")
        with self.assertRaises(ValueError):
            Scheduler().set_policy("random")

    def test_policy_alias_round_robin(self) -> None:
        """连字符与别名写法应归一化到 round_robin。"""
        scheduler = Scheduler(policy="round-robin")
        self.assertEqual(scheduler.policy, "round_robin")
        scheduler.set_policy("rr")
        self.assertEqual(scheduler.policy, "round_robin")

    def test_fifo_returns_first_candidate(self) -> None:
        """FIFO 按传入顺序选择，与能力/负载无关。"""
        candidates = [
            make_candidate("first", capability=0.1, agent_load=0.9),
            make_candidate("second", capability=0.9, agent_load=0.0),
        ]
        decision = Scheduler("fifo").pick(candidates)
        assert decision is not None
        self.assertEqual(decision.node_id, "first")
        self.assertEqual(decision.score, 1.0)
        self.assertIn("FIFO", decision.reason)

    def test_priority_picks_highest(self) -> None:
        """priority 策略选择归一化优先级最高的候选。"""
        candidates = [
            make_candidate("low", priority=1.0, agent_id="a"),
            make_candidate("high", priority=9.0, agent_id="b"),
        ]
        decision = Scheduler("priority").pick(candidates)
        assert decision is not None
        self.assertEqual(decision.node_id, "high")
        self.assertAlmostEqual(decision.score, 1.0)
        self.assertIn("priority=9.00", decision.reason)

    def test_capability_picks_best_match(self) -> None:
        """capability 策略选择能力匹配度最高者。"""
        candidates = [
            make_candidate("n1", capability=0.35, agent_id="weak"),
            make_candidate("n2", capability=0.92, agent_id="strong"),
        ]
        decision = Scheduler("capability").pick(candidates)
        assert decision is not None
        self.assertEqual(decision.node_id, "n2")
        self.assertEqual(decision.agent_id, "strong")
        self.assertAlmostEqual(decision.score, 0.92)
        self.assertIn("能力匹配", decision.reason)

    def test_load_aware_prefers_idle_agent(self) -> None:
        """load_aware 在能力相近时优先空闲 Agent。"""
        candidates = [
            make_candidate("busy", capability=0.8, agent_load=0.9, agent_id="busy"),
            make_candidate("idle", capability=0.8, agent_load=0.0, agent_id="idle"),
        ]
        decision = Scheduler("load_aware").pick(candidates)
        assert decision is not None
        self.assertEqual(decision.agent_id, "idle")
        self.assertAlmostEqual(decision.score, 0.6 * 1.0 + 0.4 * 0.8)
        self.assertIn("负载感知", decision.reason)

    def test_round_robin_cycles(self) -> None:
        """round_robin 在候选间轮转。"""
        scheduler = Scheduler("round_robin")
        candidates = [
            make_candidate("n1", agent_id="a"),
            make_candidate("n2", agent_id="b"),
        ]
        first = scheduler.pick(candidates)
        second = scheduler.pick(candidates)
        third = scheduler.pick(candidates)
        assert first and second and third
        self.assertEqual([first.node_id, second.node_id, third.node_id], ["n1", "n2", "n1"])

    def test_adaptive_formula_is_explainable(self) -> None:
        """adaptive 打分必须等于公开权重公式，且 reason / candidate_list 可解释。"""
        scheduler = Scheduler("adaptive")
        candidates = [
            make_candidate(
                "n1",
                capability=0.9,
                agent_load=0.2,
                priority=5.0,
                agent_id="agent-a",
            ),
            make_candidate(
                "n2",
                capability=0.5,
                agent_load=0.8,
                priority=1.0,
                agent_id="agent-b",
            ),
        ]
        decision = scheduler.pick(candidates)
        assert decision is not None
        weights = scheduler.describe()["weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=4)
        # n1: cap .9, idle .8, pri 1.0, 成功率未知 0.5
        expected = (
            weights["capability"] * 0.9
            + weights["idle"] * 0.8
            + weights["priority"] * 1.0
            + weights["success"] * 0.5
        )
        self.assertEqual(decision.node_id, "n1")
        self.assertAlmostEqual(decision.score, expected, places=4)
        self.assertIn("自适应策略", decision.reason)
        self.assertIn("历史成功率", decision.reason)
        self.assertEqual(len(decision.candidate_list), 2)
        for item in decision.candidate_list:
            self.assertIn("score", item)
            self.assertIn("reason", item)

    def test_adaptive_adapts_weights_after_failures(self) -> None:
        """运行期成功率偏低时提高能力权重并归一化。"""
        scheduler = Scheduler("adaptive")
        before = scheduler.describe()
        self.assertAlmostEqual(before["weights"]["capability"], 0.40)
        for _ in range(4):
            scheduler.record_outcome("agent-x", success=False)
        after = scheduler.describe()
        self.assertLess(after["global_success_rate"], 0.6)
        self.assertGreater(after["weights"]["capability"], 0.40)
        self.assertAlmostEqual(sum(after["weights"].values()), 1.0, places=4)
        self.assertEqual(after["agent_success"]["agent-x"], 0.0)

    def test_pick_filters_not_ready_and_empty(self) -> None:
        """data_ready=False 的候选被过滤；全部未就绪返回 None。"""
        scheduler = Scheduler("capability")
        self.assertIsNone(scheduler.pick([]))
        blocked = [make_candidate("n1", data_ready=False)]
        self.assertIsNone(scheduler.pick(blocked))
        mixed = [
            make_candidate("blocked", capability=0.99, data_ready=False),
            make_candidate("ready", capability=0.31, data_ready=True),
        ]
        decision = scheduler.pick(mixed)
        assert decision is not None
        self.assertEqual(decision.node_id, "ready")
        self.assertEqual(len(decision.candidate_list), 1)

    def test_describe_structure(self) -> None:
        """describe() 暴露策略、公式、权重与决策计数，供前端展示。"""
        scheduler = Scheduler("load_aware")
        info = scheduler.describe()
        self.assertEqual(info["policy"], "load_aware")
        self.assertIn("formula", info)
        self.assertIn("docs", info)
        self.assertEqual(info["decisions"], 0)
        scheduler.pick([make_candidate("n1")])
        self.assertEqual(scheduler.describe()["decisions"], 1)
        self.assertEqual(len(info["policies"]), len(POLICY_ORDER))


class PolicyNormalizationTests(unittest.TestCase):
    """2026-11-16 回归：策略别名归一化、非法策略报错、fork 策略隔离与统计共享。"""

    def test_normalize_aliases(self) -> None:
        cases = {
            "roundrobin": "round_robin",
            "round-robin": "round_robin",
            "RR": "round_robin",
            " Load ": "load_aware",
            "load": "load_aware",
            "ADAPTIVE": "adaptive",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_policy(raw), expected, raw)

    def test_normalize_invalid_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_policy("nonsense")
        message = str(ctx.exception)
        self.assertIn("未知调度策略", message)
        self.assertIn("adaptive", message)

    def test_scheduler_accepts_alias(self) -> None:
        self.assertEqual(Scheduler(policy="rr").policy, "round_robin")
        self.assertEqual(Scheduler(policy="load-aware").policy, "load_aware")

    def test_fork_isolates_policy_and_shares_stats(self) -> None:
        parent = Scheduler(policy="adaptive")
        parent.record_outcome("statistician", True)
        self.assertEqual(parent.describe()["agent_success"]["statistician"], 1.0)

        child = parent.fork("capability")
        self.assertEqual(child.policy, "capability")
        self.assertEqual(parent.policy, "adaptive")

        # 共享统计：子调度器（per-run）记录后父级 dashboard 立即可见
        child.record_outcome("data_engineer", False)
        describe = parent.describe()
        self.assertEqual(describe["agent_success"]["data_engineer"], 0.0)
        self.assertEqual(describe["global_success_rate"], 0.5)

        # 决策计数同样跨 fork 累计
        before = parent.describe()["decisions"]
        child.pick([make_candidate("n1")])
        self.assertEqual(parent.describe()["decisions"], before + 1)

    def test_fork_does_not_mutate_parent_policy(self) -> None:
        parent = Scheduler(policy="fifo")
        parent.fork("adaptive").set_policy("round_robin")
        self.assertEqual(parent.policy, "fifo")


if __name__ == "__main__":
    unittest.main()
