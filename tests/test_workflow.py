"""工作流引擎单元测试。

覆盖拓扑推进、同层并行、条件分支、JOIN 汇聚、循环节点、Agent 自循环修复、
重试、失败策略、子工作流、人工闸门、取消、模板变量与调度事件契约。
"""

from __future__ import annotations

import asyncio
import time
import unittest

from agentweave.core.types import (
    AgentResult,
    EventType,
    LoopSpec,
    RetryPolicy,
    VerifySpec,
)
from agentweave.scheduler import Scheduler
from agentweave.tools.registry import ToolResult
from agentweave.workflow import WorkflowBuilder

from ._fakes import (
    FakeCtx,
    FakeVerifier,
    agent_result,
    events_of,
    execute_workflow,
)


def linear_workflow() -> WorkflowBuilder:
    """构造 start -> plan -> report -> end 的线性工作流。"""
    builder = WorkflowBuilder("wf-linear", "线性流程")
    builder.node("start", "start")
    builder.node("plan", "agent", agent="planner", prompt="规划 {{task}}")
    builder.node("report", "agent", agent="writer", prompt="撰写报告")
    builder.node("end", "end")
    builder.edge("start", "plan")
    builder.edge("plan", "report")
    builder.edge("report", "end")
    return builder


class TopologyTest(unittest.TestCase):
    """拓扑推进与变量传播。"""

    def test_linear_workflow_runs_in_order(self) -> None:
        """线性工作流按拓扑序执行，节点输出写入 run.variables。"""
        workflow = linear_workflow().build()
        ctx = FakeCtx(config={"max_concurrent": 2})
        result, run = execute_workflow(workflow, ctx, {"task": "空气质量归因"})

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(
            [call["agent_id"] for call in ctx.agent_calls], ["planner", "writer"]
        )
        self.assertIn("空气质量归因", ctx.agent_calls[0]["prompt"])
        self.assertTrue(run.node_states["end"].output["finished"])
        for node_id in ("start", "plan", "report", "end"):
            self.assertEqual(run.node_states[node_id].status, "succeeded")

    def test_start_and_end_only(self) -> None:
        """纯结构节点（start/end）也能正常推进。"""
        builder = WorkflowBuilder("wf-structural", "结构节点")
        builder.node("start", "start")
        builder.node("end", "end")
        builder.edge("start", "end")
        result, run = execute_workflow(builder.build(), FakeCtx())
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(run.variables["start"]["started"], True)

    def test_parallel_nodes_run_concurrently(self) -> None:
        """同层就绪节点通过 gather 真并发执行，总耗时接近单节点耗时。"""

        async def slow_agent(agent_id, prompt, node, run, feedback, index):  # noqa: ANN001
            await asyncio.sleep(0.08)
            return agent_result({"agent": agent_id})

        builder = WorkflowBuilder("wf-parallel", "并行流程")
        builder.node("a", "agent", agent="researcher", prompt="检索")
        builder.node("b", "agent", agent="data_engineer", prompt="清洗")
        builder.node("join", "join")
        builder.edge("a", "join")
        builder.edge("b", "join")

        ctx = FakeCtx(agent_fn=slow_agent)
        started = time.time()
        result, run = execute_workflow(builder.build(), ctx)
        elapsed = time.time() - started

        self.assertEqual(result.status, "succeeded")
        self.assertLess(elapsed, 0.15, "并行执行耗时不应接近两个节点之和")
        self.assertEqual(len(ctx.agent_calls), 2)
        self.assertEqual(run.node_states["join"].output["count"], 2)
        self.assertEqual(set(run.node_states["join"].output["joined"]), {"a", "b"})


class ConditionTest(unittest.TestCase):
    """条件分支：真分支执行，未选中分支跳过。"""

    def _build(self) -> WorkflowBuilder:
        builder = WorkflowBuilder("wf-condition", "条件分支")
        builder.var("score", 0)
        builder.node("gate", "condition")
        builder.node("yes", "agent", agent="writer", prompt="高分报告")
        builder.node("no", "agent", agent="writer", prompt="低分说明")
        builder.edge("gate", "yes", condition="score > 5")
        builder.edge("gate", "no")
        return builder

    def test_true_branch(self) -> None:
        """score=8 命中 score > 5，走 yes 分支。"""
        builder = self._build()
        workflow = builder.build()
        workflow.variables["score"] = 8
        ctx = FakeCtx()
        result, run = execute_workflow(workflow, ctx)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual([call["node_id"] for call in ctx.agent_calls], ["yes"])
        self.assertEqual(run.node_states["no"].status, "skipped")
        skipped = events_of(ctx, EventType.NODE_SKIPPED)
        self.assertEqual(skipped[0].payload["node_id"], "no")
        self.assertIn("条件分支", skipped[0].payload["reason"])

    def test_false_branch(self) -> None:
        """score=2 未命中，走默认边 no。"""
        builder = self._build()
        workflow = builder.build()
        workflow.variables["score"] = 2
        ctx = FakeCtx()
        result, run = execute_workflow(workflow, ctx)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual([call["node_id"] for call in ctx.agent_calls], ["no"])
        self.assertEqual(run.node_states["yes"].status, "skipped")


class RetryTest(unittest.TestCase):
    """失败重试与失败策略。"""

    def _single_node(self, **kwargs) -> WorkflowBuilder:  # noqa: ANN003
        builder = WorkflowBuilder("wf-retry", "重试")
        builder.node("worker", "agent", agent="writer", prompt="执行", **kwargs)
        return builder

    def test_retry_then_success(self) -> None:
        """首次 error、第二次成功：发出 node.retrying 且最终 succeeded。"""

        def flaky(agent_id, prompt, node, run, feedback, index):  # noqa: ANN001
            if index == 0:
                return AgentResult(output=None, error="临时模型超时")
            return agent_result({"ok": True})

        builder = self._single_node(retry=RetryPolicy(max_attempts=2, backoff_s=0.0))
        ctx = FakeCtx(agent_fn=flaky)
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(ctx.agent_calls), 2)
        self.assertEqual(run.node_states["worker"].attempts, 2)
        retrying = events_of(ctx, EventType.NODE_RETRYING)
        self.assertEqual(len(retrying), 1)
        self.assertEqual(retrying[0].payload["next_attempt"], 2)
        self.assertEqual(len(events_of(ctx, EventType.NODE_STARTED)), 2)

    def test_retry_exhausted_aborts(self) -> None:
        """重试耗尽且 on_failure=abort：运行失败，后续节点不执行。"""

        def always_fail(agent_id, prompt, node, run, feedback, index):  # noqa: ANN001
            return AgentResult(output=None, error="永久失败")

        builder = WorkflowBuilder("wf-abort", "失败中止")
        builder.node(
            "worker",
            "agent",
            agent="writer",
            prompt="执行",
            retry=RetryPolicy(max_attempts=2, backoff_s=0.0),
        )
        builder.node("after", "agent", agent="writer", prompt="后续")
        builder.edge("worker", "after")

        ctx = FakeCtx(agent_fn=always_fail)
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "failed")
        self.assertIn("永久失败", result.error)
        self.assertEqual(len(ctx.agent_calls), 2)
        self.assertEqual(run.node_states["worker"].status, "failed")
        self.assertNotIn("after", run.node_states)
        self.assertEqual(len(events_of(ctx, EventType.NODE_FAILED)), 1)

    def test_on_failure_continue_keeps_downstream(self) -> None:
        """on_failure=continue：失败节点不阻断下游，但整体仍标记 failed。"""

        def fail_once(agent_id, prompt, node, run, feedback, index):  # noqa: ANN001
            if node.id == "worker":
                return AgentResult(output=None, error="可容忍失败")
            return agent_result({"ok": True})

        builder = WorkflowBuilder("wf-continue", "失败继续")
        builder.node(
            "worker",
            "agent",
            agent="writer",
            prompt="执行",
            retry=RetryPolicy(max_attempts=1, on=["error"]),
            config={"on_failure": "continue"},
        )
        builder.node("after", "agent", agent="writer", prompt="后续")
        builder.edge("worker", "after")

        ctx = FakeCtx(agent_fn=fail_once)
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "failed")
        self.assertEqual(run.node_states["worker"].status, "failed")
        self.assertEqual(run.node_states["after"].status, "succeeded")
        self.assertEqual(len(ctx.agent_calls), 2)


class LoopTest(unittest.TestCase):
    """loop 节点与 Agent 自循环。"""

    def test_loop_node_two_rounds_until_verify_passes(self) -> None:
        """循环体 analyze -> verify，首轮失败、次轮通过：反馈注入第二轮 prompt。"""
        builder = WorkflowBuilder("wf-loop", "修复循环")
        builder.node(
            "repair",
            "loop",
            loop=LoopSpec(
                max_iterations=3,
                body=["analyze", "verify"],
                feedback_var="feedback",
                mode="pdca",
            ),
        )
        builder.node(
            "analyze",
            "agent",
            agent="statistician",
            prompt="分析数据，参考反馈：{{feedback}}",
            outputs=["metrics"],
        )
        builder.node(
            "verify",
            "verify",
            verify=VerifySpec(checks=["schema"], min_confidence=0.7),
        )
        builder.edge("analyze", "verify")

        ctx = FakeCtx(verifier=FakeVerifier([False, True]))
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "succeeded", result.error)
        output = run.variables["repair"]
        self.assertEqual(output["iterations"], 2)
        self.assertTrue(output["converged"])
        self.assertTrue(output["passed"])
        self.assertEqual(run.self_loop["repair"]["iterations"], 2)
        self.assertEqual(len(ctx.agent_calls), 2)
        self.assertIn("第 1 轮修复请求", ctx.agent_calls[1]["prompt"])
        self.assertTrue(ctx.agent_calls[1]["feedback"])

        iterations = events_of(ctx, EventType.LOOP_ITERATION)
        self.assertEqual(len(iterations), 2)
        self.assertEqual(iterations[0].payload["iteration"], 1)
        self.assertFalse(iterations[0].payload["verify_passed"])
        self.assertEqual(iterations[1].payload["iteration"], 2)
        self.assertTrue(iterations[1].payload["verify_passed"])
        self.assertEqual(len(events_of(ctx, EventType.LOOP_EXIT)), 1)

    def test_loop_node_exhausted_fails(self) -> None:
        """循环始终不收敛：标记 exhausted 并按失败处理。"""
        builder = WorkflowBuilder("wf-loop-fail", "循环失败")
        builder.node(
            "repair",
            "loop",
            loop=LoopSpec(max_iterations=2, body=["analyze", "verify"]),
        )
        builder.node("analyze", "agent", agent="statistician", prompt="分析")
        builder.node(
            "verify",
            "verify",
            verify=VerifySpec(checks=["schema"], min_confidence=0.7),
        )
        builder.edge("analyze", "verify")

        ctx = FakeCtx(verifier=FakeVerifier([False, False]))
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "failed")
        output = run.variables["repair"]
        self.assertTrue(output["exhausted"])
        self.assertFalse(output["passed"])
        self.assertEqual(run.self_loop["repair"]["stop_reason"], "max_iterations")

    def test_agent_self_loop_repairs_output(self) -> None:
        """Agent 节点挂 verify + loop：首轮不通过、反馈注入后第二轮通过。"""
        builder = WorkflowBuilder("wf-selfloop", "Agent 自修复")
        builder.node(
            "analyze",
            "agent",
            agent="statistician",
            prompt="输出 JSON。修复反馈：{{feedback}}",
            outputs=["metrics"],
            verify=VerifySpec(checks=["schema"], min_confidence=0.7),
            loop=LoopSpec(max_iterations=2, feedback_var="feedback", mode="pdca"),
        )

        ctx = FakeCtx(verifier=FakeVerifier([False, True]))
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(ctx.agent_calls), 2)
        self.assertTrue(ctx.agent_calls[1]["feedback"])
        self.assertIn("第 1 轮修复请求", ctx.agent_calls[1]["prompt"])
        self.assertEqual(run.node_states["analyze"].status, "succeeded")
        self.assertEqual(run.self_loop["analyze"]["iterations"], 2)
        self.assertTrue(run.self_loop["analyze"]["converged"])


class StructureAndSideEffectTest(unittest.TestCase):
    """子工作流、人工闸门、工具节点、取消与调度事件。"""

    def test_subworkflow_nested_execution(self) -> None:
        """subworkflow 节点在子 RunState 中递归执行并合并节点状态。"""
        child_builder = WorkflowBuilder("wf-child", "子流程")
        child_builder.node("work", "agent", agent="researcher", prompt="子任务")
        child = child_builder.build()

        parent_builder = WorkflowBuilder("wf-parent", "父流程")
        parent_builder.node("sub", "subworkflow", workflow="wf-child")
        parent_builder.node("after", "agent", agent="writer", prompt="汇总")
        parent_builder.edge("sub", "after")

        ctx = FakeCtx()
        ctx.workflows["wf-child"] = child
        result, run = execute_workflow(parent_builder.build(), ctx)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertIn("sub.work", run.node_states)
        self.assertEqual(run.node_states["sub.work"].status, "succeeded")
        self.assertEqual(run.variables["sub"]["workflow"], "wf-child")
        self.assertEqual(ctx.agent_calls[0]["agent_id"], "researcher")

    def test_human_gate_rejected_aborts(self) -> None:
        """人工审核拒绝：节点失败、运行按 abort 结束。"""
        builder = WorkflowBuilder("wf-gate", "人工闸门")
        builder.node("gate", "human_gate")
        builder.node("report", "agent", agent="writer", prompt="报告")
        builder.edge("gate", "report")

        ctx = FakeCtx(human_decision=False)
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "failed")
        self.assertEqual(ctx.human_calls, ["gate"])
        self.assertEqual(run.node_states["gate"].status, "failed")
        self.assertIn("人工审核未通过", run.node_states["gate"].error)
        self.assertEqual(events_of(ctx, EventType.NODE_FAILED)[0].payload["node_id"], "gate")

    def test_tool_node_renders_args_and_emits_events(self) -> None:
        """tool 节点渲染模板参数，并发出 tool.called / tool.result。"""
        builder = WorkflowBuilder("wf-tool", "工具节点")
        builder.var("threshold", 5)
        builder.node(
            "scan",
            "tool",
            tool="dataset_stats",
            args={"path": "{{dataset_path}}", "limit": "{{threshold}}"},
        )

        ctx = FakeCtx(
            tool_fn=lambda name, args: ToolResult.success({"rows": 10}),
        )
        result, run = execute_workflow(
            builder.build(), ctx, {"dataset_path": "examples/data.csv"}
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(ctx.tool_calls[0]["name"], "dataset_stats")
        self.assertEqual(ctx.tool_calls[0]["args"]["path"], "examples/data.csv")
        self.assertEqual(str(ctx.tool_calls[0]["args"]["limit"]), "5")
        self.assertEqual(run.variables["scan"]["rows"], 10)
        called = events_of(ctx, EventType.TOOL_CALLED)
        result_events = events_of(ctx, EventType.TOOL_RESULT)
        self.assertEqual(called[0].payload["name"], "dataset_stats")
        self.assertTrue(result_events[0].payload["ok"])

    def test_cancel_stops_execution(self) -> None:
        """执行中请求取消：后续节点不再调度，状态为 cancelled。"""
        holder: dict[str, FakeCtx] = {}

        def cancelling(agent_id, prompt, node, run, feedback, index):  # noqa: ANN001
            holder["ctx"].cancel()
            return agent_result({"ok": True})

        builder = WorkflowBuilder("wf-cancel", "取消")
        builder.node("first", "agent", agent="planner", prompt="第一步")
        builder.node("second", "agent", agent="writer", prompt="第二步")
        builder.edge("first", "second")

        ctx = FakeCtx(agent_fn=cancelling)
        holder["ctx"] = ctx
        result, run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(run.status, "cancelled")
        self.assertEqual([call["node_id"] for call in ctx.agent_calls], ["first"])
        state = run.node_states.get("second")
        self.assertTrue(state is None or state.status == "pending")

    def test_scheduler_events_are_explainable(self) -> None:
        """plan.ready / node.scheduled 暴露 score、reason、agent_load。"""
        scheduler = Scheduler(policy="adaptive")
        builder = WorkflowBuilder("wf-sched", "调度观测")
        builder.node("a", "agent", agent="planner", prompt="规划", config={"priority": 5})
        builder.node("b", "agent", agent="writer", prompt="写作")
        builder.edge("a", "b")

        ctx = FakeCtx(scheduler=scheduler, config={"max_concurrent": 2})
        result, _run = execute_workflow(builder.build(), ctx)

        self.assertEqual(result.status, "succeeded", result.error)
        plans = events_of(ctx, EventType.PLAN_READY)
        self.assertEqual(len(plans), 1)
        payload = plans[0].payload
        self.assertEqual(payload["policy"], "adaptive")
        self.assertEqual(len(payload["candidates"]), 2)
        for candidate in payload["candidates"]:
            self.assertTrue(
                {"node_id", "priority", "agent_id", "score", "reason"}
                <= set(candidate)
            )

        scheduled = events_of(ctx, EventType.NODE_SCHEDULED)
        self.assertEqual(len(scheduled), 2)
        for event in scheduled:
            payload = event.payload
            self.assertIn("score", payload)
            self.assertIn("reason", payload)
            self.assertIn("agent_load", payload)
            self.assertGreaterEqual(payload["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
