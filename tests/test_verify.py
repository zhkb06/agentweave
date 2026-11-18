"""验证流水线单元测试。

覆盖 L1 结构 / L1 非空 / L2 真值 / L3 独立复算 / L4 跨 Agent / L5 统计与
LLM 降级，以及加权置信度、硬性失败、跳过不计分母、自定义检查器注册和
``run.verification`` / ``verify.result`` 事件契约。
"""

from __future__ import annotations

import asyncio
import unittest

from agentweave.core.types import (
    AgentResult,
    EventType,
    VerifyCheckResult,
    VerifySpec,
    WorkflowNode,
)
from agentweave.runtime.state import RunState
from agentweave.tools.registry import ToolResult
from agentweave.verify import VerificationPipeline, VerifyContext
from agentweave.verify.checkers import (
    check_code_rerun,
    check_cross_agent,
    check_non_empty,
    check_numeric_ground_truth,
    check_schema,
    check_statistical,
)

from ._fakes import FakeCtx


def make_node(outputs: list[str] | None = None, node_id: str = "analyze") -> WorkflowNode:
    """构造验证用节点。"""
    return WorkflowNode(id=node_id, type="agent", outputs=list(outputs or []))


def make_run() -> RunState:
    """构造验证用 RunState。"""
    return RunState(run_id="run-verify", workflow_id="wf-verify", workflow_name="验证测试")


def run_async(coro):  # noqa: ANN001, ANN202 - 测试辅助
    """在独立事件循环中执行协程。"""
    return asyncio.run(coro)


def analysis_output(wind: float, r2: float = 0.62, n: int = 2000) -> dict:
    """构造 statistician 风格的输出（含 code / metrics）。"""
    code = (
        "import json\n"
        "metrics = {'coefficients': {'wind': %s}, 'r2': %s, 'n': %s}\n"
        "print(json.dumps(metrics))\n" % (wind, r2, n)
    )
    return {
        "summary": "多元线性回归完成",
        "confidence": 0.82,
        "code": code,
        "metrics": {
            "coefficients": {
                "temp": -0.84,
                "humidity": 0.41,
                "wind": wind,
                "so2": 0.30,
            },
            "r2": r2,
            "n": n,
            "p_values": {"temp": 0.001, "humidity": 0.02, "wind": 0.0005, "so2": 0.03},
            "conclusion": "各因子显著",
        },
    }


class StructuralCheckTest(unittest.TestCase):
    """L1 结构与非空检查。"""

    def test_schema_pass_and_missing_field(self) -> None:
        """schema 要求 JSON 对象且包含 outputs 声明字段。"""
        node = make_node(["metrics", "code"])
        spec = VerifySpec(checks=["schema"])
        good = run_async(
            check_schema(VerifyContext(node=node, output={"metrics": {}, "code": "x"}, run=make_run(), spec=spec))
        )
        self.assertTrue(good.passed)
        self.assertIn("契约字段齐全", good.detail)

        bad = run_async(
            check_schema(VerifyContext(node=node, output={"metrics": {}}, run=make_run(), spec=spec))
        )
        self.assertFalse(bad.passed)
        self.assertIn("缺少契约字段", bad.detail)
        self.assertIn("code", bad.evidence["missing"])

        text = run_async(
            check_schema(VerifyContext(node=node, output="这是纯文本，不是 JSON 对象", run=make_run(), spec=spec))
        )
        self.assertFalse(text.passed)
        self.assertIn("不是 JSON 对象", text.detail)

    def test_non_empty_threshold(self) -> None:
        """文本输出至少 20 字符，容器输出不能为空。"""
        node = make_node()
        spec = VerifySpec(checks=["non_empty"])
        short = run_async(
            check_non_empty(VerifyContext(node=node, output="太短", run=make_run(), spec=spec))
        )
        self.assertFalse(short.passed)
        long_text = "这是一段长度足够的分析结论，用于满足非空检查的二十字符门槛。"
        ok = run_async(
            check_non_empty(VerifyContext(node=node, output=long_text, run=make_run(), spec=spec))
        )
        self.assertTrue(ok.passed)
        empty = run_async(
            check_non_empty(VerifyContext(node=node, output={}, run=make_run(), spec=spec))
        )
        self.assertFalse(empty.passed)


class GroundTruthCheckTest(unittest.TestCase):
    """L2 真值比对检查。"""

    def setUp(self) -> None:
        self.node = make_node(["metrics"])
        self.run = make_run()
        self.truth = {
            "coefficients": {
                "temp": -0.85,
                "humidity": 0.42,
                "wind": -1.60,
                "so2": 0.28,
            },
        }

    def test_skip_when_ground_truth_absent(self) -> None:
        """未注入真值时跳过并说明原因，不崩溃。"""
        spec = VerifySpec(checks=["numeric_ground_truth"], ground_truth=None)
        result = run_async(
            check_numeric_ground_truth(
                VerifyContext(node=self.node, output=analysis_output(-1.05), run=self.run, spec=spec)
            )
        )
        self.assertTrue(result.passed)
        self.assertTrue(result.evidence.get("skipped"))
        self.assertIn("未注入真值", result.detail)

    def test_reports_claimed_vs_truth_rel_error(self) -> None:
        """失败 detail 必须含申报值 / 真值 / 相对误差，供前端验证中心展示。"""
        spec = VerifySpec(
            checks=["numeric_ground_truth"],
            ground_truth=self.truth,
            tolerance=0.15,
        )
        result = run_async(
            check_numeric_ground_truth(
                VerifyContext(node=self.node, output=analysis_output(-1.05), run=self.run, spec=spec)
            )
        )
        self.assertFalse(result.passed)
        self.assertIn("容差 15", result.detail)  # 中文反馈包含容差口径
        self.assertIn("相对误差 34.4%", result.detail)
        self.assertIn("申报 -1.05", result.detail)
        self.assertIn("-1.6", result.detail)
        self.assertIn("34.4%", result.detail)
        failures = result.evidence["failures"]
        self.assertTrue(any(item["path"] == "coefficients.wind" for item in failures))
        wind = next(item for item in failures if item["path"] == "coefficients.wind")
        self.assertAlmostEqual(wind["actual"], -1.05)
        self.assertAlmostEqual(wind["expected"], -1.6)
        self.assertAlmostEqual(wind["rel_error"], 0.34375, places=5)

    def test_second_round_within_tolerance(self) -> None:
        """修复后 -1.55 vs 真值 -1.60 相对误差 3.1%，通过。"""
        spec = VerifySpec(
            checks=["numeric_ground_truth"],
            ground_truth=self.truth,
            tolerance=0.15,
        )
        result = run_async(
            check_numeric_ground_truth(
                VerifyContext(node=self.node, output=analysis_output(-1.55), run=self.run, spec=spec)
            )
        )
        self.assertTrue(result.passed)
        self.assertLess(result.evidence["max_rel_error"], 0.15)


class CodeRerunCheckTest(unittest.TestCase):
    """L3 独立复算检查。"""

    def setUp(self) -> None:
        self.node = make_node(["code", "metrics"])
        self.run = make_run()
        self.spec = VerifySpec(checks=["code_rerun"], tolerance=0.15)
        self.ctx = FakeCtx()

    def _tool_ctx(self, value_json: dict) -> FakeCtx:
        """构造返回指定复算 JSON 的 python_exec 桩。"""
        return FakeCtx(tool_fn=lambda name, args: ToolResult.success({"value_json": value_json}))

    def test_requires_code_and_tools(self) -> None:
        """缺少 code 或工具注册表时跳过而不是崩溃。"""
        no_code = run_async(
            check_code_rerun(
                VerifyContext(
                    node=self.node,
                    output={"metrics": {"r2": 0.6}},
                    run=self.run,
                    spec=self.spec,
                    ctx=self.ctx,
                )
            )
        )
        self.assertTrue(no_code.evidence.get("skipped"))
        self.assertIn("code 字段", no_code.detail)

        no_tools_ctx = FakeCtx()  # tool_fn 未配置 -> ctx.tools is None
        skip_tools = run_async(
            check_code_rerun(
                VerifyContext(
                    node=self.node,
                    output=analysis_output(-1.05),
                    run=self.run,
                    spec=self.spec,
                    ctx=no_tools_ctx,
                )
            )
        )
        self.assertTrue(skip_tools.evidence.get("skipped"))
        self.assertIn("ctx.tools", skip_tools.detail)

    def test_declared_vs_recomputed_mismatch_fails(self) -> None:
        """申报 -1.05、复算 -1.60，偏差 34% 应判失败。"""
        ctx = self._tool_ctx({"coefficients": {"wind": -1.60}})
        result = run_async(
            check_code_rerun(
                VerifyContext(
                    node=self.node,
                    output=analysis_output(-1.05),
                    run=self.run,
                    spec=self.spec,
                    ctx=ctx,
                )
            )
        )
        self.assertFalse(result.passed)
        self.assertIn("独立重跑不一致", result.detail)
        self.assertEqual(ctx.tool_calls[0]["name"], "python_exec")
        self.assertIn("coefficients.wind", result.evidence["failures"][0]["path"])

    def test_recomputed_match_passes(self) -> None:
        """申报 -1.55、复算 -1.60，偏差 3.1% 在 15% 容差内。"""
        ctx = self._tool_ctx({"coefficients": {"wind": -1.60}})
        result = run_async(
            check_code_rerun(
                VerifyContext(
                    node=self.node,
                    output=analysis_output(-1.55),
                    run=self.run,
                    spec=self.spec,
                    ctx=ctx,
                )
            )
        )
        self.assertTrue(result.passed)
        self.assertIn("独立重跑通过", result.detail)


class CrossAgentAndStatisticalTest(unittest.TestCase):
    """L4 跨 Agent 与 L5 统计检查。"""

    def test_cross_agent_compares_recomputed_metrics(self) -> None:
        """复核 Agent 返回的 metrics 与申报一致则通过，否则失败。"""
        node = make_node(["metrics"])
        run = make_run()
        output = analysis_output(-1.55)
        spec = VerifySpec(
            checks=["cross_agent"],
            tolerance=0.15,
            config={"verifier_agent": "verifier"},
        )

        def good_agent(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            return AgentResult(output={"metrics": {"coefficients": {"wind": -1.6}}}, confidence=0.9)

        ctx = FakeCtx(agent_fn=good_agent)
        passed = run_async(
            check_cross_agent(VerifyContext(node=node, output=output, run=run, spec=spec, ctx=ctx))
        )
        self.assertTrue(passed.passed)
        self.assertEqual(ctx.agent_calls[0]["agent_id"], "verifier")
        self.assertEqual(ctx.agent_calls[0]["context"].get("phase"), "cross_verify")

        def bad_agent(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            return AgentResult(output={"metrics": {"coefficients": {"wind": -0.5}}}, confidence=0.9)

        bad_ctx = FakeCtx(agent_fn=bad_agent)
        failed = run_async(
            check_cross_agent(
                VerifyContext(node=node, output=output, run=run, spec=spec, ctx=bad_ctx)
            )
        )
        self.assertFalse(failed.passed)
        self.assertIn("交叉验证不一致", failed.detail)

    def test_statistical_thresholds(self) -> None:
        """n≥1000、R²≥0.55 时通过；样本不足 / 拟合差则失败。"""
        node = make_node(["metrics"])
        run = make_run()
        spec = VerifySpec(
            checks=["statistical"],
            config={"thresholds": {"n_min": 1000, "r2_min": 0.55}},
        )
        good = run_async(
            check_statistical(
                VerifyContext(node=node, output=analysis_output(-1.55), run=run, spec=spec)
            )
        )
        self.assertTrue(good.passed)
        self.assertIn("n=2000≥1000", good.detail)

        bad_output = analysis_output(-1.55, r2=0.40, n=800)
        bad = run_async(
            check_statistical(
                VerifyContext(node=node, output=bad_output, run=run, spec=spec)
            )
        )
        self.assertFalse(bad.passed)
        self.assertIn("统计阈值未满足", bad.detail)

        skipped = run_async(
            check_statistical(
                VerifyContext(node=node, output=bad_output, run=run, spec=VerifySpec(checks=["statistical"]))
            )
        )
        self.assertTrue(skipped.evidence.get("skipped"))


class PipelineTest(unittest.TestCase):
    """流水线汇总：加权置信度 / 硬性失败 / 记录 / 事件。"""

    def setUp(self) -> None:
        self.node = make_node(["metrics"])
        self.run = make_run()

    def _pipeline(self) -> tuple[VerificationPipeline, FakeCtx]:
        ctx = FakeCtx()
        pipeline = VerificationPipeline()
        pipeline.set_context(ctx)
        return pipeline, ctx

    def test_unknown_checker_is_hard_failure(self) -> None:
        """未知检查器判失败并标记硬性失败，整体不通过。"""
        pipeline, _ctx = self._pipeline()
        spec = VerifySpec(checks=["schema", "no_such_checker"], min_confidence=0.7)
        result = run_async(pipeline.verify(spec, self.node, {"metrics": {}}, self.run))
        self.assertFalse(result.passed)
        self.assertTrue(result.metrics["hard_fail"])
        self.assertEqual(result.metrics["checks_failed"], 1)
        self.assertIn("未知检查器", result.feedback)

    def test_skip_not_counted_in_denominator(self) -> None:
        """跳过的检查不计入置信度分母：schema 通过 + 真值跳过 => 置信度 1.0。"""
        pipeline, _ctx = self._pipeline()
        spec = VerifySpec(checks=["schema", "numeric_ground_truth"], min_confidence=0.7)
        result = run_async(pipeline.verify(spec, self.node, {"metrics": {}}, self.run))
        self.assertTrue(result.passed)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.metrics["checks_skipped"], 1)

    def test_weighted_confidence_and_feedback(self) -> None:
        """部分失败时置信度按权重折算，feedback 含期望/实际/建议。"""
        pipeline, _ctx = self._pipeline()
        spec = VerifySpec(
            checks=["schema", "numeric_ground_truth"],
            ground_truth={"coefficients": {"wind": -1.60}},
            tolerance=0.15,
            min_confidence=0.7,
        )
        result = run_async(
            pipeline.verify(spec, self.node, analysis_output(-1.05), self.run)
        )
        self.assertFalse(result.passed)
        self.assertGreater(result.confidence, 0.0)
        self.assertLess(result.confidence, 0.7)
        self.assertIn("【验证未通过】", result.feedback)
        self.assertIn("coefficients.wind", result.feedback)
        self.assertIn("建议", result.feedback)

    def test_records_and_emits_verify_result(self) -> None:
        """每次验证追加 run.verification 并发 verify.result 事件。"""
        pipeline, ctx = self._pipeline()
        spec = VerifySpec(checks=["schema"])
        result = run_async(pipeline.verify(spec, self.node, {"metrics": {}}, self.run))
        self.assertTrue(result.passed)
        self.assertEqual(len(self.run.verification), 1)
        record = self.run.verification[0]
        self.assertEqual(record["passed"], True)
        self.assertIn("checks", record)
        events = [event for event in ctx.events if event.type == EventType.VERIFY_RESULT]
        self.assertEqual(len(events), 1)
        payload = events[0].payload
        self.assertEqual(payload["node_id"], self.node.id)
        self.assertIn("confidence", payload)
        self.assertTrue(payload["checks"])
        self.assertIn("detail", payload["checks"][0])

    def test_custom_checker_registration(self) -> None:
        """自定义检查器可注册、可带权重，并参与加权置信度。"""
        pipeline, _ctx = self._pipeline()

        async def always_fail(vc: VerifyContext) -> VerifyCheckResult:
            return VerifyCheckResult(name="always_fail", passed=False, detail="自定义失败")

        pipeline.register("always_fail", always_fail, weight=3.0, description="自定义失败检查器")
        self.assertIn("always_fail", pipeline.available())
        describe = {item["name"]: item for item in pipeline.describe()}
        self.assertEqual(describe["always_fail"]["weight"], 3.0)
        self.assertFalse(describe["always_fail"]["builtin"])

        spec = VerifySpec(checks=["schema", "always_fail"], min_confidence=0.7)
        result = run_async(pipeline.verify(spec, self.node, {"metrics": {}}, self.run))
        self.assertFalse(result.passed)
        self.assertAlmostEqual(result.confidence, round(0.8 / (0.8 + 3.0), 4))
        self.assertTrue(pipeline.unregister("always_fail"))
        self.assertNotIn("always_fail", pipeline.available())

    def test_llm_judge_degrades_offline(self) -> None:
        """离线无 LLM 时 llm_judge 跳过且不拉低置信度。"""
        pipeline, _ctx = self._pipeline()
        spec = VerifySpec(checks=["schema", "llm_judge"])
        result = run_async(pipeline.verify(spec, self.node, {"metrics": {}}, self.run))
        self.assertTrue(result.passed)
        judge = next(check for check in result.checks if check.name == "llm_judge")
        self.assertTrue(judge.evidence.get("skipped"))
        self.assertIn("离线", judge.detail)


if __name__ == "__main__":
    unittest.main()
