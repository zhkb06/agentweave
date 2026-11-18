"""LLM 层测试：MockProvider 多轮反馈、ModelRouter 路由/降级、OpenAI 兼容请求构造。

覆盖点
------
1. ``MockProvider`` 确定性输出、七大角色 JSON 契约、statistician 首轮/修复轮系数变化、
   verifier 对偏差申报的反馈、真实可运行的分析代码。
2. ``ModelRouter`` 的 fast/smart 路由、``router``/``routing`` 配置兼容、失败重试后
   回退离线 Mock、用量统计与脱敏状态。
3. ``OpenAICompatProvider`` 的请求构造（路径/鉴权/body/tools/response_format）、
   HTTP 错误分类与指数退避重试——全部使用本地 ``http.server`` 假服务，零外网。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agentweave.llm.base import (
    LLMAuthError,
    LLMBadRequestError,
    LLMProvider,
    LLMResult,
    LLMServerError,
)
from agentweave.llm.mock import (
    FIXED_COEFFICIENTS,
    FIRST_ROUND_COEFFICIENTS,
    MockProvider,
)
from agentweave.llm.openai_compat import OpenAICompatProvider
from agentweave.llm.router import ModelRouter


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
def _messages(role: str, user: str = "请开始处理本节点任务。") -> list[dict[str, str]]:
    """构造带角色元信息的 messages（Mock 依赖 ``role=xxx`` 识别角色）。"""
    return [
        {"role": "system", "content": f"你是{role}。只输出 JSON，不要输出解释文字。role={role}"},
        {"role": "user", "content": user},
    ]


def _parse(result: LLMResult) -> dict[str, Any]:
    """把 LLM 返回的 content 解析成 dict。"""
    payload = json.loads(result.content)
    assert isinstance(payload, dict), f"content 不是 JSON 对象: {result.content[:200]}"
    return payload


class _JsonHandler(BaseHTTPRequestHandler):
    """极简 OpenAI 兼容假服务：记录请求并按服务端配置返回响应。"""

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        server = self.server  # type: ignore[assignment]
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        server.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if server.fail_remaining > 0:
            server.fail_remaining -= 1
            self._reply(500, {"error": {"message": "upstream boom", "type": "server_error"}})
            return
        if server.forced_status >= 400:
            self._reply(server.forced_status, {"error": {"message": "unauthorized", "type": "auth_error"}})
            return
        self._reply(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": server.response_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok": true, "answer": 42}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # noqa: D401 - 静默测试日志
        """静默 http.server 的访问日志。"""


class MockProviderTest(unittest.IsolatedAsyncioTestCase):
    """确定性 Mock 提供方：离线演示的核心。"""

    async def test_planner_contract(self) -> None:
        """planner 输出 tasks / success_criteria，且确定性可复现。"""
        provider = MockProvider()
        first = await provider.complete(_messages("planner"))
        second = await provider.complete(_messages("planner"))
        self.assertEqual(first.content, second.content, "Mock 必须确定性可复现")
        payload = _parse(first)
        self.assertIn("summary", payload)
        self.assertTrue(payload["summary"])
        self.assertIsInstance(payload["tasks"], list)
        self.assertGreaterEqual(len(payload["tasks"]), 4)
        self.assertIsInstance(payload["success_criteria"], list)
        first_task = payload["tasks"][0]
        for field in ("id", "goal", "owner"):
            self.assertIn(field, first_task)
        self.assertGreaterEqual(float(payload["confidence"]), 0.0)

    async def test_data_engineer_contract(self) -> None:
        """data_engineer 输出数据路径、行数与清洗日志。"""
        payload = _parse(await MockProvider().complete(_messages("data_engineer")))
        self.assertEqual(payload["rows"], 2000)
        self.assertIn("air_quality.csv", payload["data_path"])
        self.assertIsInstance(payload["cleaning_log"], list)
        self.assertTrue(payload["cleaning_log"])

    async def test_writer_report_markdown(self) -> None:
        """writer 产出中文 Markdown 报告（≥300 字）与标题。"""
        payload = _parse(await MockProvider().complete(_messages("writer")))
        self.assertTrue(payload["title"])
        markdown = payload["markdown"]
        self.assertGreaterEqual(len(markdown), 300, "研究报告正文必须≥300 字")
        self.assertTrue(markdown.lstrip().startswith("#"), "Markdown 应以一级标题开头")

    async def test_statistician_first_round_biased(self) -> None:
        """首轮 statistician 给出故意偏差的系数，并附带真实可运行的分析代码。"""
        payload = _parse(await MockProvider().complete(_messages("statistician")))
        metrics = payload["metrics"]
        coefficients = metrics["coefficients"]
        for feature, expected in FIRST_ROUND_COEFFICIENTS.items():
            self.assertAlmostEqual(coefficients[feature], expected, places=6)
        self.assertEqual(metrics["n"], 2000)
        self.assertGreater(metrics["r2"], 0.6)  # 现由真实数据 OLS 计算（约 0.7138）
        self.assertEqual(set(metrics["p_values"]), set(FIRST_ROUND_COEFFICIENTS))
        self.assertTrue(all(value < 0.05 for value in metrics["p_values"].values()))
        code = payload["code"]
        self.assertIn("import json", code)
        self.assertIn("air_quality.csv", code)
        self.assertIn("print(", code)

    async def test_statistician_repairs_after_feedback(self) -> None:
        """收到验证反馈后，statistician 上报接近真值的系数。"""
        messages = _messages("statistician")
        messages.append(
            {
                "role": "user",
                "content": "【上一轮验证反馈】wind 系数偏差 37%，超过 15% 容差，请重新拟合。",
            }
        )
        payload = _parse(await MockProvider().complete(messages))
        coefficients = payload["metrics"]["coefficients"]
        for feature, expected in FIXED_COEFFICIENTS.items():
            self.assertAlmostEqual(coefficients[feature], expected, places=6)
        self.assertGreater(payload["metrics"]["r2"], 0.6)  # 真实 OLS 计算，不再固定 0.72
        self.assertGreaterEqual(float(payload["confidence"]), 0.8)

    async def test_verifier_detects_deviation(self) -> None:
        """verifier 对偏差申报给出 agreement=False 与可执行中文反馈。"""
        claim = json.dumps({"coefficients": dict(FIRST_ROUND_COEFFICIENTS)}, ensure_ascii=False)
        messages = _messages("verifier", f"statistician 申报的回归结果：{claim}")
        payload = _parse(await MockProvider().complete(messages))
        self.assertFalse(payload["agreement"])
        self.assertIn("feedback", payload)
        self.assertTrue(payload["feedback"])
        deviations = payload["deviations"]
        self.assertIn("wind", deviations)
        self.assertGreater(deviations["wind"]["rel_error"], 0.15)
        self.assertAlmostEqual(payload["recomputed"]["coefficients"]["wind"], -1.6728, places=4)

    async def test_verifier_confirms_repair(self) -> None:
        """修复后的申报值与复算一致，verifier 返回 agreement=True。"""
        claim = json.dumps(
            {"coefficients": dict(FIXED_COEFFICIENTS), "r2": 0.72, "n": 2000},
            ensure_ascii=False,
        )
        messages = _messages("verifier", f"statistician 修复后的结果：{claim}")
        payload = _parse(await MockProvider().complete(messages))
        self.assertTrue(payload["agreement"])
        self.assertEqual(payload["feedback"], "")
        self.assertEqual(payload["recomputed"]["n"], 2000)

    async def test_react_tool_call_before_final(self) -> None:
        """提供工具且尚无观察时，statistician 先发起 python_exec 调用。"""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "python_exec",
                    "description": "执行标准库 Python 代码",
                    "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
                },
            }
        ]
        provider = MockProvider()
        payload = _parse(await provider.complete(_messages("statistician"), tools=tools))
        self.assertEqual(payload["tool"], "python_exec")
        self.assertIn("code", payload["args"])
        self.assertTrue(payload["thought"])
        # 回注工具观察后，才输出最终结果
        messages = _messages("statistician")
        messages.append(
            {"role": "tool", "tool_call_id": "call-1", "content": '{"coefficients": {"wind": -1.6728}}'}
        )
        final = _parse(await provider.complete(messages, tools=tools))
        self.assertIn("metrics", final)

    async def test_estimate_tokens_and_list_models(self) -> None:
        """Mock 报告正数 token 用量与可用模型列表。"""
        provider = MockProvider()
        result = await provider.complete(_messages("critic"))
        self.assertGreaterEqual(result.tokens_in, 1)
        self.assertGreaterEqual(result.tokens_out, 1)
        self.assertEqual(result.provider, provider.name)
        self.assertTrue(await provider.list_models())


class ModelRouterTest(unittest.IsolatedAsyncioTestCase):
    """路由、重试与降级。"""

    async def test_role_routing_and_usage(self) -> None:
        """按 llm.router 映射选择 fast/smart，并统计调用次数。"""
        router = ModelRouter(
            {
                "provider": "mock",
                "router": {"statistician": "smart", "critic": "fast", "default": "fast"},
            }
        )
        self.assertEqual(router.tier_for("statistician"), "smart")
        self.assertEqual(router.tier_for("critic"), "fast")
        self.assertEqual(router.tier_for("unknown-role"), "fast")

        result = await router.complete(_messages("statistician"), role="statistician")
        self.assertTrue(result.content)
        usage = router.usage()
        self.assertEqual(usage["calls"], 1)
        self.assertGreaterEqual(usage["tokens_in"], 1)
        self.assertIn("by_model", usage)

        status = router.status()
        self.assertIn("api_key_set", status)
        self.assertNotIn("sk-", json.dumps(status, ensure_ascii=False))

    async def test_routing_alias_key(self) -> None:
        """兼容旧的 ``routing`` 配置键名。"""
        router = ModelRouter({"provider": "mock", "routing": {"default": "fast"}})
        self.assertEqual(router.tier_for("planner"), "fast")

    async def test_fallback_to_mock_on_failure(self) -> None:
        """模型持续失败时回退离线 Mock，保证工作流不中断。"""

        class _BrokenProvider(LLMProvider):
            """总是抛错的提供方。"""

            name = "broken"

            async def complete(self, messages, tools=None, **kwargs):  # noqa: D401, ANN001
                raise LLMServerError("模拟上游 500")

            async def list_models(self):  # noqa: D401
                return []

        router = ModelRouter({"provider": "mock", "max_retries": 0, "router": {"default": "smart"}})
        router.install_provider("smart", _BrokenProvider())
        result = await router.complete(_messages("statistician"), role="statistician")
        self.assertEqual(result.model, "mock-fallback")
        self.assertTrue(result.content)
        self.assertTrue((result.raw or {}).get("fallback"))
        usage = router.usage()
        self.assertGreaterEqual(usage["fallback_calls"], 1)
        self.assertGreaterEqual(usage["errors"], 1)

    async def test_retry_before_fallback(self) -> None:
        """可重试错误会按指数退避重试（max_retries 内）。"""

        class _FlakyProvider(LLMProvider):
            """前 N 次失败、之后成功的提供方。"""

            name = "flaky"

            def __init__(self) -> None:
                self.attempts = 0

            async def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001
                self.attempts += 1
                if self.attempts < 3:
                    raise LLMServerError("暂时不可用")
                return LLMResult(content='{"ok": true}', model="flaky", tokens_in=1, tokens_out=1)

            async def list_models(self):  # noqa: D401
                return ["flaky"]

        router = ModelRouter({"provider": "mock", "max_retries": 2, "router": {"default": "smart"}})
        provider = _FlakyProvider()
        router.install_provider("smart", provider)
        result = await router.complete(_messages("critic"), role="critic")
        self.assertEqual(provider.attempts, 3)
        self.assertEqual(result.model, "flaky")  # 未触发 Mock 回退
        self.assertEqual(router.usage()["calls"], 1)

    async def test_remote_provider_without_key_degrades(self) -> None:
        """远程 provider 缺少 Key 时主动降级为 Mock，且状态脱敏。"""
        router = ModelRouter(
            {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key_env": "AGENTWEAVE_TEST_NONEXISTENT_KEY",
            }
        )
        self.assertTrue(router.degraded)
        status = router.status()
        self.assertTrue(status["degraded"])
        self.assertNotIn("api_key", {k for k in status if k == "api_key"})
        result = await router.complete(_messages("writer"), role="writer")
        self.assertIn("mock", result.model)


class OpenAICompatProviderTest(unittest.IsolatedAsyncioTestCase):
    """OpenAI 兼容协议：请求构造、错误分类、重试。"""

    def _start_server(self, *, fail_remaining: int = 0, forced_status: int = 200):
        handler = type("Handler", (_JsonHandler,), {})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.requests = []  # type: ignore[attr-defined]
        server.fail_remaining = fail_remaining  # type: ignore[attr-defined]
        server.forced_status = forced_status  # type: ignore[attr-defined]
        server.response_model = "gpt-4o-mini"  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        return server, url

    async def test_request_construction(self) -> None:
        """校验请求路径、Bearer 鉴权、tools 与 response_format。"""
        server, url = self._start_server()
        provider = OpenAICompatProvider(
            base_url=url, api_key="test-key", model="gpt-4o-mini", timeout=5.0, max_retries=0
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "回声",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                },
            }
        ]
        result = await provider.complete(
            [{"role": "user", "content": "你好"}], tools=tools, response_format="json"
        )
        self.assertEqual(len(server.requests), 1)
        record = server.requests[0]
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(record["headers"].get("authorization"), "Bearer test-key")
        body = record["body"]
        self.assertEqual(body["model"], "gpt-4o-mini")
        self.assertEqual(body["messages"][0]["content"], "你好")
        self.assertEqual(body["tools"][0]["function"]["name"], "echo")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertAlmostEqual(result.tokens_in, 11)
        self.assertAlmostEqual(result.tokens_out, 7)
        self.assertIn("ok", result.content)
        self.assertGreaterEqual(result.latency_s, 0.0)
        self.assertTrue(result.model)

    async def test_auth_error_classification(self) -> None:
        """401 归类为不可重试的 LLMAuthError。"""
        server, url = self._start_server(forced_status=401)
        provider = OpenAICompatProvider(base_url=url, api_key="bad-key", timeout=5.0, max_retries=2)
        with self.assertRaises(LLMAuthError):
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(server.requests), 1, "鉴权错误不应重试")

    async def test_error_types(self) -> None:
        """错误类继承关系与 retryable 标记正确。"""
        from agentweave.llm.base import LLMError

        self.assertTrue(issubclass(LLMAuthError, LLMError))
        self.assertTrue(issubclass(LLMBadRequestError, LLMError))
        self.assertTrue(issubclass(LLMServerError, LLMError))
        self.assertTrue(LLMServerError("x").retryable)
        self.assertFalse(LLMAuthError("x").retryable)

    async def test_retry_on_server_error(self) -> None:
        """5xx 可重试：失败后指数退避重试成功。"""
        server, url = self._start_server(fail_remaining=1)
        provider = OpenAICompatProvider(
            base_url=url, api_key="test-key", timeout=5.0, max_retries=2, backoff_base=0.01
        )
        result = await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(server.requests), 2)
        self.assertIn("ok", result.content)

    async def test_missing_key_and_describe(self) -> None:
        """未配置 Key 时 has_key 为 False，describe 不泄露明文。"""
        provider = OpenAICompatProvider(
            api_key="", model="gpt-4o-mini", api_key_env="AGENTWEAVE_TEST_MISSING_KEY_XYZ"
        )
        self.assertFalse(provider.has_key)
        info = provider.describe()
        self.assertFalse(info["api_key_set"])
        self.assertNotIn("api_key_env", json.dumps(info).replace("api_key_set", ""))

    async def test_base_url_trailing_slash(self) -> None:
        """base_url 末尾斜杠被规范化。"""
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:1/v1/", api_key="k")
        self.assertEqual(provider.base_url, "http://127.0.0.1:1/v1")

    async def test_timeout_alias_accepted(self) -> None:
        """兼容平台配置使用的 timeout_s 关键字。"""
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:1/v1", api_key="k", timeout_s=12)
        self.assertEqual(provider.timeout, 12.0)


if __name__ == "__main__":
    unittest.main()
