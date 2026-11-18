"""智能体层测试：ReAct 循环、工具调用、记忆、Agent 注册表、Mock 闭环。

覆盖点
------
1. ``AgentRuntime`` 单轮输出、多轮工具调用（tool_calls / iterations / 事件）、
   迭代耗尽兜底、feedback 只有非空时才拼入 prompt。
2. ``AgentMemory`` 记录与召回、``AgentRegistry`` 加载 7 个 Agent / 版本历史 /
   匹配 / 并发配额。
3. Mock 闭环：statistician 首轮偏差系数 → 收到 feedback 后修正——离线演示的核心链路。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentweave.agents.agent import AgentRuntime, extract_json
from agentweave.agents.memory import AgentMemory
from agentweave.agents.registry import AgentRegistry, load_agents
from agentweave.core.events import EventBus
from agentweave.core.types import AgentSpec, EventType, ToolSpec
from agentweave.llm.base import LLMProvider, LLMResult
from agentweave.llm.mock import MockProvider
from agentweave.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENTS_CONFIG = PROJECT_ROOT / "config" / "agents.json"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeLLM(LLMProvider):
    """按顺序返回预设 JSON 文本的假模型；记录每次收到的 messages。"""

    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: Any = None,
        **kwargs: Any,
    ) -> LLMResult:
        self.calls.append([dict(message) for message in messages])
        content = self.responses.pop(0) if self.responses else "{}"
        return LLMResult(
            content=content,
            model=self.name,
            tokens_in=5,
            tokens_out=7,
            latency_s=0.001,
            provider=self.name,
        )

    async def list_models(self) -> list[str]:
        return [self.name]


def _spec(agent_id: str = "tester", tools: list[str] | None = None, max_iterations: int = 3) -> AgentSpec:
    """构造最小可用的 AgentSpec。"""
    return AgentSpec(
        id=agent_id,
        name="测试代理",
        role="tester",
        system_prompt="你是测试代理。只输出 JSON，不要输出解释文字。",
        description="单元测试用代理",
        skills=["测试"],
        tools=list(tools or []),
        max_iterations=max_iterations,
    )


class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """ReAct 运行时行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _echo_registry(self) -> ToolRegistry:
        registry = ToolRegistry(workspace_dir=self.tmp.name)
        registry.register(
            ToolSpec(
                name="echo",
                description="回声工具",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            lambda text: {"echo": text},
        )
        return registry

    async def test_extract_json_variants(self) -> None:
        """兼容裸 JSON、```json``` 围栏与前后夹杂文字。"""
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(extract_json('分析如下：{"a": 3} 完毕'), {"a": 3})
        self.assertIsNone(extract_json("没有 JSON"))

    async def test_single_round_output(self) -> None:
        """无工具调用时一轮产出最终 JSON。"""
        llm = FakeLLM(['{"thought": "直接回答", "summary": "分析完成", "evidence": ["数据齐全"], "confidence": 0.9}'])
        runtime = AgentRuntime(spec=_spec(), llm=llm)
        result = await runtime.run(prompt="请分析数据", run_id="run-1", node_id="analyze")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.output["summary"], "分析完成")
        self.assertAlmostEqual(result.confidence, 0.9)
        self.assertEqual(result.tokens_in, 5)
        self.assertEqual(result.tokens_out, 7)
        self.assertIsNone(result.error)

    async def test_tool_call_then_final(self) -> None:
        """先发起工具调用、回注观察，再给出最终结论。"""
        llm = FakeLLM(
            [
                '{"thought": "需要回声", "tool": "echo", "args": {"text": "hi"}}',
                '{"thought": "已拿到观察", "summary": "回声验证通过", "evidence": ["echo=hi"], "confidence": 0.85}',
            ]
        )
        runtime = AgentRuntime(spec=_spec(tools=["echo"]), llm=llm, tools=self._echo_registry())
        result = await runtime.run(prompt="请调用回声", run_id="run-2", node_id="act")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(len(result.tool_calls), 1)
        record = result.tool_calls[0]
        self.assertEqual(record.name, "echo")
        self.assertEqual(record.args, {"text": "hi"})
        self.assertEqual(record.result, {"echo": "hi"})
        self.assertIsNone(record.error)
        self.assertEqual(result.output["summary"], "回声验证通过")
        # 第二轮 LLM 调用应看到 tool 观察消息
        second_call = llm.calls[1]
        self.assertTrue(any(message.get("role") == "tool" for message in second_call))
        self.assertTrue(any("hi" in str(message.get("content")) for message in second_call))

    async def test_unauthorized_tool_rejected(self) -> None:
        """未授权工具不应执行，而是把错误回注给模型。"""
        llm = FakeLLM(
            [
                '{"tool": "echo", "args": {"text": "nope"}}',
                '{"summary": "工具不可用", "confidence": 0.4}',
            ]
        )
        runtime = AgentRuntime(spec=_spec(tools=[]), llm=llm, tools=self._echo_registry())
        result = await runtime.run(prompt="请调用工具", run_id="run-3", node_id="act")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertIn("授权", result.tool_calls[0].error or "")

    async def test_max_iterations_exhausted(self) -> None:
        """模型一直要求工具时，达到上限并给出明确错误。"""
        llm = FakeLLM(['{"tool": "echo", "args": {"text": "loop"}}'] * 5)
        runtime = AgentRuntime(spec=_spec(tools=["echo"], max_iterations=2), llm=llm, tools=self._echo_registry())
        result = await runtime.run(prompt="死循环", run_id="run-4", node_id="loop")
        self.assertEqual(result.iterations, 2)
        self.assertIsNotNone(result.error)
        self.assertIn("最大迭代次数", result.error or "")
        self.assertLessEqual(result.confidence, 0.5)

    async def test_feedback_only_when_present(self) -> None:
        """feedback 为空时 prompt 不得出现反馈标记（否则 Mock 会误判修复轮）。"""
        final = '{"summary": "完成", "confidence": 0.8}'
        llm = FakeLLM([final, final])
        runtime = AgentRuntime(spec=_spec(), llm=llm)
        await runtime.run(prompt="任务 A", run_id="run-5", node_id="n1")
        first_prompt = "\n".join(message["content"] for message in llm.calls[0])
        self.assertNotIn("【上一轮验证反馈】", first_prompt)

        await runtime.run(
            prompt="任务 A",
            run_id="run-5",
            node_id="n1",
            feedback="wind 系数偏差 37%，请重新拟合。",
        )
        second_prompt = "\n".join(message["content"] for message in llm.calls[1])
        self.assertIn("【上一轮验证反馈】", second_prompt)
        self.assertIn("请重新拟合", second_prompt)

    async def test_events_emitted(self) -> None:
        """每轮发 agent.thought，结束时发 agent.decision。"""
        bus = EventBus()
        llm = FakeLLM(['{"thought": "先看数据", "summary": "完成", "confidence": 0.7}'])
        runtime = AgentRuntime(spec=_spec(), llm=llm, bus=bus)
        await runtime.run(prompt="任务", run_id="run-6", node_id="think")
        events = bus.history("run-6")
        types = [event.type for event in events]
        self.assertIn(EventType.AGENT_THOUGHT, types)
        self.assertIn(EventType.AGENT_DECISION, types)
        thought = next(event for event in events if event.type == EventType.AGENT_THOUGHT)
        self.assertEqual(thought.agent_id, "tester")
        self.assertIn("text", thought.payload)

    async def test_memory_records_result(self) -> None:
        """运行结果写入 AgentMemory，可被后续节点召回。"""
        memory = AgentMemory(run_id="run-7")
        llm = FakeLLM(['{"summary": "回归系数已修正为 -1.67", "confidence": 0.88}'])
        runtime = AgentRuntime(spec=_spec(), llm=llm, memory=memory)
        await runtime.run(prompt="回归分析", run_id="run-7", node_id="analyze")
        self.assertGreaterEqual(len(memory), 1)
        recalled = memory.recall("回归系数")
        self.assertTrue(recalled)
        self.assertIn("-1.67", " ".join(recalled))
        self.assertTrue(memory.summary())

    async def test_mock_statistician_first_round_and_repair(self) -> None:
        """Mock 闭环：首轮偏差系数 → 反馈后接近真值。"""
        spec = AgentSpec(
            id="statistician",
            name="统计建模专家",
            role="statistician",
            system_prompt="你是统计建模专家。只输出 JSON，不要输出解释文字。",
            tools=[],
            max_iterations=4,
        )
        runtime = AgentRuntime(spec=spec, llm=MockProvider())
        first = await runtime.run(prompt="请对空气质量数据做多元线性回归", run_id="run-8", node_id="analyze")
        coefficients = first.output["metrics"]["coefficients"]
        self.assertAlmostEqual(coefficients["wind"], -1.05, places=6)
        self.assertAlmostEqual(coefficients["temp"], -0.62, places=6)
        self.assertLess(first.confidence, 0.8)

        second = await runtime.run(
            prompt="请对空气质量数据做多元线性回归",
            run_id="run-8",
            node_id="analyze",
            feedback="独立复算发现 wind 系数偏差 37%，超过容差，请重新拟合后汇报。",
        )
        fixed = second.output["metrics"]["coefficients"]
        self.assertAlmostEqual(fixed["wind"], -1.67, places=6)
        self.assertAlmostEqual(fixed["temp"], -0.84, places=6)
        self.assertAlmostEqual(fixed["humidity"], 0.44, places=6)
        self.assertAlmostEqual(fixed["so2"], 0.26, places=6)
        self.assertGreaterEqual(second.confidence, 0.8)
        self.assertIn("code", second.output)
        self.assertIn("air_quality.csv", second.output["code"])


class AgentRegistryTest(unittest.IsolatedAsyncioTestCase):
    """Agent 注册表。"""

    async def test_load_agents_config(self) -> None:
        """加载 config/agents.json 得到 7 个 Agent。"""
        registry = load_agents(str(AGENTS_CONFIG))
        self.assertEqual(len(registry), 7)
        spec = registry.get("statistician")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.id, "statistician")
        self.assertIn("python_exec", spec.tools)
        for agent_id in ("planner", "researcher", "data_engineer", "verifier", "critic", "writer"):
            self.assertIn(agent_id, registry)

    async def test_reload_and_load_method(self) -> None:
        """reload 与 load(path) 均可重新读取配置。"""
        registry = load_agents(str(AGENTS_CONFIG))
        self.assertEqual(registry.reload(), 7)
        fresh = AgentRegistry()
        self.assertEqual(fresh.load(str(AGENTS_CONFIG)), 7)
        self.assertEqual(len(fresh), 7)

    async def test_match_by_skill(self) -> None:
        """按技能模糊匹配，返回 (AgentSpec, 分数) 且按分数降序。"""
        registry = load_agents(str(AGENTS_CONFIG))
        matches = registry.match(["多元回归"], ["python_exec"])
        self.assertTrue(matches)
        spec, score = matches[0]
        self.assertEqual(spec.id, "statistician")
        self.assertGreater(score, 0.0)
        scores = [item[1] for item in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))

    async def test_versions_history(self) -> None:
        """同一 Agent 的新版本会保留历史。"""
        registry = AgentRegistry()
        registry.register(
            AgentSpec(id="demo", name="演示代理", role="tester", system_prompt="v1", version="1.0.0")
        )
        registry.register(
            AgentSpec(id="demo", name="演示代理", role="tester", system_prompt="v2", version="1.1.0")
        )
        self.assertEqual(registry.get("demo").version, "1.1.0")
        self.assertGreaterEqual(len(registry.history("demo")), 2)
        self.assertEqual(len(registry.list()), 1)

    async def test_concurrency_quota(self) -> None:
        """非阻塞并发配额：acquire 成功后可 release，超限返回 False。"""
        registry = load_agents(str(AGENTS_CONFIG))
        self.assertTrue(registry.acquire("writer", max_concurrency=1))
        self.assertFalse(registry.acquire("writer", max_concurrency=1))
        registry.release("writer")
        self.assertTrue(registry.acquire("writer", max_concurrency=1))
        registry.release("writer")
        report = registry.load_report()
        self.assertTrue(isinstance(report, list))
        self.assertTrue(any(item["agent_id"] == "writer" for item in report))


if __name__ == "__main__":
    unittest.main()
