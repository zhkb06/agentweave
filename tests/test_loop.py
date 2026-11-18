"""PDCA 自循环控制器单元测试。

覆盖停止条件（通过 / 轮数耗尽 / 停滞）、结构化反馈生成（复用流水线 feedback
或从检查证据重建）、历史记录与摘要契约，确保「验证失败 → 反馈注入 → 重跑 →
收敛」闭环可被解释与观测。
"""

from __future__ import annotations

import unittest

from agentweave.core.types import (
    LoopSpec,
    VerifyCheckResult,
    VerifyResult,
)
from agentweave.loop import LoopState, SelfLoopController


def failed_verify(
    confidence: float = 0.45,
    checks: list[VerifyCheckResult] | None = None,
    feedback: str = "",
) -> VerifyResult:
    """构造一个未通过的验证结果。"""
    return VerifyResult(
        passed=False,
        confidence=confidence,
        checks=list(checks or []),
        feedback=feedback,
        metrics={"min_confidence": 0.7},
    )


def passed_verify(confidence: float = 0.92) -> VerifyResult:
    """构造一个通过的验证结果。"""
    return VerifyResult(passed=True, confidence=confidence, checks=[], feedback="")


def wind_failure() -> VerifyCheckResult:
    """构造 wind 系数偏差的失败检查（带证据）。"""
    return VerifyCheckResult(
        name="numeric_ground_truth",
        passed=False,
        detail="wind 系数偏差 34.4%（申报 -1.05，真值 -1.60）",
        evidence={
            "expected": {"coefficients": {"wind": -1.6}},
            "actual": {"coefficients": {"wind": -1.05}},
            "suggestion": "重新拟合并核对量纲 / 共线性",
            "failures": [{"path": "coefficients.wind", "rel_error": 0.34375}],
        },
    )


class LoopStateTest(unittest.TestCase):
    """LoopState 初始状态。"""

    def test_begin_returns_initial_state(self) -> None:
        """begin() 返回干净状态：0 轮、空历史、未收敛。"""
        controller = SelfLoopController(LoopSpec(max_iterations=3))
        state = controller.begin()
        self.assertIsInstance(state, LoopState)
        self.assertEqual(state.iteration, 0)
        self.assertEqual(state.history, [])
        self.assertFalse(state.converged)
        self.assertFalse(state.exhausted)
        self.assertEqual(state.stop_reason, "running")
        self.assertGreater(state.started_at, 0.0)


class StopConditionTest(unittest.TestCase):
    """should_continue 的三种停止条件。"""

    def test_pass_stops_immediately(self) -> None:
        """验证通过即收敛，不再重跑。"""
        controller = SelfLoopController(LoopSpec(max_iterations=3))
        state = controller.begin()
        self.assertFalse(controller.should_continue(passed_verify(), state))
        self.assertTrue(state.converged)
        self.assertEqual(state.stop_reason, "converged")

    def test_continues_until_max_iterations(self) -> None:
        """连续失败时最多跑 max_iterations 轮，然后标记耗尽。"""
        spec = LoopSpec(max_iterations=3)
        controller = SelfLoopController(spec)
        state = controller.begin()
        rounds = 0
        verify = failed_verify(0.4)
        while controller.should_continue(verify, state):
            controller.record(state, verify, {"iteration": state.iteration})
            rounds += 1
            verify = failed_verify(0.45 + 0.1 * rounds)  # 每轮确有提升，避免停滞判定
            self.assertLessEqual(rounds, 5)
        self.assertEqual(rounds, 3)
        self.assertTrue(state.exhausted)
        self.assertFalse(state.converged)
        self.assertEqual(state.stop_reason, "max_iterations")

    def test_stagnation_stops_early(self) -> None:
        """连续两轮置信度提升低于阈值即判停滞，节省 token。"""
        controller = SelfLoopController(LoopSpec(max_iterations=10))
        state = controller.begin()
        for confidence in (0.30, 0.305, 0.307):
            controller.record(state, failed_verify(confidence))
        self.assertFalse(controller.should_continue(failed_verify(0.307), state))
        self.assertTrue(state.exhausted)
        self.assertEqual(state.stop_reason, "stagnation")

    def test_improvement_resets_stagnation(self) -> None:
        """明显提升的轮次不会触发停滞。"""
        controller = SelfLoopController(LoopSpec(max_iterations=10))
        state = controller.begin()
        for confidence in (0.30, 0.31, 0.50):
            controller.record(state, failed_verify(confidence))
        self.assertTrue(controller.should_continue(failed_verify(0.50), state))


class FeedbackTest(unittest.TestCase):
    """build_feedback 的中文、可执行、含证据要求。"""

    def test_reuses_pipeline_feedback(self) -> None:
        """流水线已生成反馈时优先复用，并加轮次抬头。"""
        controller = SelfLoopController()
        state = controller.begin()
        verify = failed_verify(
            0.55,
            checks=[wind_failure()],
            feedback="【验证未通过】coefficients.wind 相对误差 34.4%，请重新拟合。",
        )
        text = controller.build_feedback(verify, state)
        self.assertIn("【第 1 轮修复请求】", text)
        self.assertIn("coefficients.wind", text)
        self.assertIn("请仅针对上述失败项修正", text)
        self.assertEqual(state.feedback, text)

    def test_rebuilds_from_check_evidence(self) -> None:
        """无 feedback 时从失败检查重建：失败项 + 期望 + 实际 + 建议。"""
        controller = SelfLoopController()
        state = controller.begin()
        verify = failed_verify(0.45, checks=[wind_failure()], feedback="")
        text = controller.build_feedback(verify, state)
        self.assertIn("numeric_ground_truth", text)
        self.assertIn("申报 -1.05", text)
        self.assertIn("期望", text)
        self.assertIn("-1.6", text)
        self.assertIn("实际", text)
        self.assertIn("建议", text)
        self.assertIn("量纲", text)

    def test_skipped_checks_not_blamed(self) -> None:
        """跳过的检查不应出现在修复请求中。"""
        skipped = VerifyCheckResult(
            name="llm_judge",
            passed=True,
            detail="离线降级跳过",
            evidence={"skipped": True, "reason": "离线"},
        )
        controller = SelfLoopController()
        text = controller.build_feedback(
            failed_verify(0.5, checks=[skipped, wind_failure()], feedback=""),
            controller.begin(),
        )
        self.assertNotIn("llm_judge", text)
        self.assertIn("numeric_ground_truth", text)


class RecordAndSummaryTest(unittest.TestCase):
    """record / summary 的可观测契约。"""

    def test_record_appends_history_entry(self) -> None:
        """每轮记录 iteration / passed / confidence / failures / improvement。"""
        controller = SelfLoopController(LoopSpec(max_iterations=3))
        state = controller.begin()
        first = controller.record(state, failed_verify(0.45, checks=[wind_failure()]), {"a": 1})
        self.assertEqual(first["iteration"], 1)
        self.assertEqual(first["failures"], ["numeric_ground_truth"])
        self.assertAlmostEqual(first["confidence"], 0.45)
        self.assertIn("output_preview", first)
        self.assertEqual(len(state.history), 1)

        second = controller.record(state, passed_verify(0.86), {"b": 2})
        self.assertEqual(second["iteration"], 2)
        self.assertAlmostEqual(second["improvement"], 0.41, places=2)
        self.assertTrue(state.converged)
        self.assertEqual(state.stop_reason, "converged")
        self.assertAlmostEqual(state.best_confidence, 0.86)

    def test_summary_contract(self) -> None:
        """summary 暴露前端时间线需要的字段。"""
        spec = LoopSpec(max_iterations=4, mode="pdca", feedback_var="feedback")
        controller = SelfLoopController(spec)
        state = controller.begin()
        controller.record(state, failed_verify(0.4, checks=[wind_failure()]))
        controller.record(state, passed_verify(0.9))
        summary = controller.summary(state)
        self.assertEqual(summary["iterations"], 2)
        self.assertEqual(summary["iteration"], 2)
        self.assertEqual(summary["max_iterations"], 4)
        self.assertTrue(summary["converged"])
        self.assertFalse(summary["exhausted"])
        self.assertEqual(summary["mode"], "pdca")
        self.assertEqual(summary["feedback_var"], "feedback")
        self.assertEqual(len(summary["history"]), 2)
        self.assertEqual(summary["history"][0]["failures"], ["numeric_ground_truth"])
        self.assertGreaterEqual(summary["duration_s"], 0.0)

    def test_summary_without_state(self) -> None:
        """未开始循环时给出 not_started 摘要而不是崩溃。"""
        summary = SelfLoopController().summary(None)
        self.assertEqual(summary["iterations"], 0)
        self.assertEqual(summary["stop_reason"], "not_started")
        self.assertEqual(summary["history"], [])


if __name__ == "__main__":
    unittest.main()
