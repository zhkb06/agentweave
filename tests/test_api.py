"""AgentWeave Web API 集成测试。

不依赖真实平台：用 ``FakePlatform`` 实现门面契约，``create_app`` 绑定 127.0.0.1:0
真实端口，用 ``http.client`` 走完整 HTTP 链路（含 SSE 首帧回放、心跳、并发）。

运行::

    PYTHONIOENCODING=utf-8 py -3.14 -m unittest tests.test_api -v
"""
from __future__ import annotations

import http.client
import json
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.api.app import DEFAULT_CONFIG, ApiError, create_app  # noqa: E402
from agentweave.core.types import RunRequest  # noqa: E402

RUN_ID = "run-test-001"
WORKFLOW_ID = "wf-ai4s-air-quality"
NOW = time.time()

RUN_SUMMARY: dict[str, Any] = {
    "run_id": RUN_ID,
    "workflow_id": WORKFLOW_ID,
    "workflow_name": "AI4S 空气质量与气象因子归因分析",
    "label": "演示运行",
    "status": "succeeded",
    "created_at": NOW - 30,
    "started_at": NOW - 29,
    "finished_at": NOW - 17,
    "policy": "adaptive",
    "nodes": {
        "scan": {
            "node_id": "scan",
            "status": "succeeded",
            "agent_id": "data_engineer",
            "confidence": 0.9,
            "tokens_in": 120,
            "tokens_out": 90,
        },
        "analyze": {
            "node_id": "analyze",
            "status": "succeeded",
            "agent_id": "statistician",
            "confidence": 0.93,
            "tokens_in": 300,
            "tokens_out": 210,
        },
    },
    "node_states": {"scan": {"node_id": "scan", "status": "succeeded"}},
    "metrics": {
        "duration_s": 12.3,
        "tokens_total": 1234,
        "avg_confidence": 0.93,
        "success_rate": 1.0,
    },
    "verification": [
        {
            "node_id": "verify",
            "layer": "numeric",
            "passed": True,
            "confidence": 0.93,
            "checks": [
                {"name": "numeric_ground_truth", "passed": True, "detail": "相对误差 4.2%"},
                {"name": "code_rerun", "passed": True, "detail": "复算一致"},
            ],
        }
    ],
    "self_loop": {
        "node_id": "analyze",
        "iterations": [
            {
                "iteration": 1,
                "confidence": 0.62,
                "passed": False,
                "failures": ["numeric_ground_truth"],
                "feedback_preview": "R² 未达标，需修正特征",
            },
            {"iteration": 2, "confidence": 0.93, "passed": True, "failures": []},
        ],
    },
    "artifacts": [{"name": "report.md", "kind": "markdown", "size": 12}],
    "variables": {},
    "inputs": {"question": "空气质量与气象因子归因"},
}

EVENTS: list[dict[str, Any]] = [
    {
        "seq": 1,
        "type": "run.started",
        "run_id": RUN_ID,
        "node_id": None,
        "agent_id": None,
        "payload": {
            "workflow_id": WORKFLOW_ID,
            "workflow_name": "AI4S 空气质量与气象因子归因分析",
            "policy": "adaptive",
        },
        "level": "info",
        "ts": NOW - 29,
    },
    {
        "seq": 2,
        "type": "node.started",
        "run_id": RUN_ID,
        "node_id": "scan",
        "agent_id": "data_engineer",
        "payload": {"node_id": "scan", "agent_id": "data_engineer", "attempt": 1},
        "level": "info",
        "ts": NOW - 28,
    },
    {
        "seq": 3,
        "type": "node.scheduled",
        "run_id": RUN_ID,
        "node_id": "analyze",
        "agent_id": "statistician",
        "payload": {
            "node_id": "analyze",
            "agent_id": "statistician",
            "score": 0.91,
            "reason": "能力匹配 + 当前负载低",
            "agent_load": 0.2,
        },
        "level": "info",
        "ts": NOW - 27,
    },
    {
        "seq": 4,
        "type": "verify.result",
        "run_id": RUN_ID,
        "node_id": "verify",
        "agent_id": "verifier",
        "payload": {
            "node_id": "verify",
            "passed": True,
            "confidence": 0.93,
            "checks": [{"name": "code_rerun", "passed": True, "detail": "复算一致"}],
        },
        "level": "info",
        "ts": NOW - 20,
    },
    {
        "seq": 5,
        "type": "run.finished",
        "run_id": RUN_ID,
        "node_id": None,
        "agent_id": None,
        "payload": {
            "status": "succeeded",
            "duration_s": 12.3,
            "nodes": 10,
            "tokens": 1234,
            "success_rate": 1.0,
            "self_loop_iterations": 1,
        },
        "level": "info",
        "ts": NOW - 17,
    },
]


class FakeSubscription:
    """模拟平台订阅对象：``get(timeout)`` 返回事件或 None，``close()`` 清理。"""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        for event in events or []:
            self._queue.put(event)
        self.closed = False

    def get(self, timeout: float = 30.0) -> dict[str, Any] | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


class FakePlatform:
    """平台门面契约的最小可用实现（同步/异步方法混用，覆盖 asyncio 桥）。"""

    def __init__(self) -> None:
        self.started = False
        self.shutdown_called = False
        self.runs: dict[str, dict[str, Any]] = {}
        self.start_requests: list[RunRequest] = []
        self.llm_payloads: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.last_subscription: FakeSubscription | None = None
        self.llm = {
            "provider": "mock",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-test-1234567890abcd",
            "api_key_set": True,
            "latency_ms": 12.3,
            "status": "ready",
        }
        self.agents = [
            {
                "id": "planner",
                "name": "规划者",
                "role": "planner",
                "skills": ["任务分解", "调度"],
                "tools": ["dataset_stats"],
                "temperature": 0.2,
                "version": "1.0.0",
                "enabled": True,
            },
            {
                "id": "statistician",
                "name": "统计学家",
                "role": "statistician",
                "skills": ["回归分析", "假设检验"],
                "tools": ["python_exec"],
                "temperature": 0.1,
                "version": "1.0.0",
                "enabled": True,
            },
        ]
        self.tools = [
            {
                "name": "dataset_stats",
                "description": "统计数据集行列数、缺失值",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            {
                "name": "python_exec",
                "description": "执行 Python 代码沙盒",
                "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
            },
        ]

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    # -- 运行 -----------------------------------------------------------
    def start_run(self, request: RunRequest) -> str:
        self.start_requests.append(request)
        self.runs[RUN_ID] = dict(RUN_SUMMARY)
        return RUN_ID

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id == "run-missing":
            return None
        return self.runs.get(run_id) or dict(RUN_SUMMARY)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        runs = self.runs or {RUN_ID: dict(RUN_SUMMARY)}
        return list(runs.values())[:limit]

    async def cancel_run(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True

    def get_events(self, run_id: str, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return [event for event in EVENTS if int(event.get("seq") or 0) > since_seq][:limit]

    def subscribe(self, run_id: str, last_event_id: int = 0) -> FakeSubscription:
        events = [event for event in EVENTS if int(event.get("seq") or 0) > last_event_id]
        self.last_subscription = FakeSubscription(events)
        return self.last_subscription

    # -- 工作流 ---------------------------------------------------------
    def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": WORKFLOW_ID,
                "name": "AI4S 空气质量与气象因子归因分析",
                "version": "1.0.0",
                "description": "演示工作流",
                "node_count": 11,
                "tags": ["ai4s", "demo"],
                "owner": "agentweave",
            }
        ]

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        if workflow_id != WORKFLOW_ID:
            return None
        return {
            "id": WORKFLOW_ID,
            "name": "AI4S 空气质量与气象因子归因分析",
            "nodes": [
                {"id": "start", "type": "start", "label": "开始"},
                {"id": "plan", "type": "agent", "label": "规划", "agent": "planner"},
                {"id": "verify", "type": "verify", "label": "验证", "agent": "verifier"},
            ],
            "edges": [
                {"source": "start", "target": "plan"},
                {"source": "plan", "target": "verify"},
            ],
        }

    async def save_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    # -- Agent / 工具 ---------------------------------------------------
    def list_agents(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "load": [
                {"agent_id": "planner", "inflight": 1, "utilization": 0.5},
                {"agent_id": "statistician", "inflight": 0, "utilization": 0.0},
            ],
        }

    def save_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.agents.append(dict(payload))
        return dict(payload)

    def list_tools(self) -> dict[str, Any]:
        return {
            "tools": self.tools,
            "mcp_servers": [
                {"name": "weather-mcp", "url": "http://127.0.0.1:9900", "status": "connected"}
            ],
        }

    # -- 验证 / 报告 ----------------------------------------------------
    def get_verification(self, run_id: str) -> dict[str, Any] | None:
        return {"run_id": run_id, "checks": [], "confidence": 0.93}

    def get_loop_status(self, run_id: str) -> dict[str, Any]:
        return dict(RUN_SUMMARY["self_loop"])

    def get_report(self, run_id: str) -> dict[str, Any] | None:
        return None

    def event_stats(self) -> dict[str, Any]:
        return {"events": len(EVENTS), "runs_tracked": 1}

    # -- 模型设置 -------------------------------------------------------
    def llm_status(self) -> dict[str, Any]:
        return dict(self.llm)

    async def set_llm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.llm_payloads.append(dict(payload))
        self.llm.update({key: value for key, value in payload.items() if key != "test"})
        return dict(self.llm)


class HeartbeatPlatform(FakePlatform):
    """运行中的 run + 空事件流：用于验证 15s 心跳与并发不阻塞（测试里用 0.2s 心跳）。"""

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return {
            "run_id": run_id,
            "status": "running",
            "workflow_id": WORKFLOW_ID,
            "nodes": {},
        }

    def get_events(self, run_id: str, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return []

    def subscribe(self, run_id: str, last_event_id: int = 0) -> FakeSubscription:
        self.last_subscription = FakeSubscription([])
        return self.last_subscription


class LegacySubscribePlatform(FakePlatform):
    """模拟真实平台门面的旧订阅签名 ``subscribe(run_id)``（不带 last_event_id）。"""

    def subscribe(self, run_id: str) -> FakeSubscription:
        self.last_subscription = FakeSubscription([])
        return self.last_subscription


class ServerTestCase(unittest.TestCase):
    """每个用例独立起一个真实 HTTP 服务（127.0.0.1:0）。"""

    platform: FakePlatform
    config_overrides: dict[str, Any] = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agentweave-test-"))
        self.web_dir = self.tmp / "web"
        self.web_dir.mkdir()
        (self.web_dir / "index.html").write_text(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            "<title>织流 AgentWeave</title></head><body>WEB-OK</body></html>",
            encoding="utf-8",
        )
        (self.web_dir / "app.js").write_text("// 织流前端\n", encoding="utf-8")
        self.workspace = self.tmp / "workspace"
        (self.workspace / "runs" / RUN_ID / "artifacts").mkdir(parents=True)
        (self.workspace / "runs" / RUN_ID / "artifacts" / "report.md").write_text(
            "# 归因报告\n\npm25 与 temp 强负相关。\n", encoding="utf-8"
        )
        self.platform = self.platform_class()
        config = dict(DEFAULT_CONFIG)
        config.update(
            {
                "host": "127.0.0.1",
                "port": 0,
                "web_dir": str(self.web_dir),
                "workspace_dir": str(self.workspace),
                "sse_heartbeat_s": 15.0,
            }
        )
        config.update(self.config_overrides)
        self.server = create_app(self.platform, config)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop_server)

    platform_class = FakePlatform

    def _stop_server(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.server.app.shutdown()  # type: ignore[attr-defined]
            self.thread.join(timeout=5)
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- HTTP 工具 ------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        port: int | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=timeout)
        payload = raw_body
        request_headers = {"Accept": "application/json"}
        if payload is None and body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            request_headers.update(headers)
        try:
            conn.request(method, path, body=payload, headers=request_headers)
            response = conn.getresponse()
            data = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, data
        finally:
            conn.close()

    def get_json(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        status, _, data = self.request("GET", path, **kwargs)
        return status, json.loads(data.decode("utf-8")) if data else None

    def post_json(self, path: str, body: Any, **kwargs: Any) -> tuple[int, Any]:
        status, _, data = self.request("POST", path, body=body, **kwargs)
        return status, json.loads(data.decode("utf-8")) if data else None

    def assert_json_error(self, status: int, payload: Any, expected_status: int, code: str) -> None:
        self.assertEqual(status, expected_status, payload)
        self.assertIsInstance(payload, dict)
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["code"], code)
        self.assertTrue(payload["error"]["message"])


# ---------------------------------------------------------------------------
# 系统 / 概览
# ---------------------------------------------------------------------------
class HealthTests(ServerTestCase):
    def test_health(self) -> None:
        status, payload = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertGreaterEqual(payload["agents"], 2)
        self.assertGreater(payload["uptime_s"], 0)
        self.assertTrue(payload["platform"]["ready"])
        self.assertTrue(self.platform.started)
        # API Key 打码，完整 Key 不得出现在响应中
        self.assertEqual(payload["llm"]["api_key"], "sk-t****abcd")
        self.assertNotIn("1234567890", json.dumps(payload, ensure_ascii=False))

    def test_overview(self) -> None:
        status, payload = self.get_json("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"], 2)
        self.assertEqual(payload["tools"], 2)
        self.assertEqual(payload["workflows"], 1)
        self.assertEqual(payload["runs"], 1)
        self.assertEqual(payload["success_rate"], 1.0)
        self.assertEqual(payload["avg_confidence"], 0.93)
        self.assertEqual(payload["events"], len(EVENTS))
        self.assertTrue(payload["recent_runs"])
        self.assertTrue(payload["recent_events"])
        self.assertEqual(payload["recent_runs"][0]["run_id"], RUN_ID)
        self.assertTrue(payload["agent_load"])


# ---------------------------------------------------------------------------
# Agent / 工作流 / 工具
# ---------------------------------------------------------------------------
class AgentWorkflowToolTests(ServerTestCase):
    def test_agents_passthrough(self) -> None:
        status, payload = self.get_json("/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["agents"]), 2)
        self.assertEqual(payload["agents"][0]["id"], "planner")
        self.assertEqual(payload["load"][0]["inflight"], 1)

    def test_agent_save(self) -> None:
        status, payload = self.post_json(
            "/api/agents", {"id": "critic", "name": "批评家", "role": "critic"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["id"], "critic")
        self.assertEqual(len(self.platform.agents), 3)

    def test_agent_save_requires_id(self) -> None:
        status, payload = self.post_json("/api/agents", {"name": "无 id"})
        self.assert_json_error(status, payload, 400, "INVALID_AGENT")

    def test_workflows(self) -> None:
        status, payload = self.get_json("/api/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(payload["workflows"][0]["id"], WORKFLOW_ID)
        self.assertEqual(payload["workflows"][0]["node_count"], 11)

    def test_workflow_detail(self) -> None:
        status, payload = self.get_json(f"/api/workflows/{WORKFLOW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], WORKFLOW_ID)
        self.assertEqual(len(payload["nodes"]), 3)
        self.assertEqual(payload["edges"][0]["source"], "start")

    def test_workflow_not_found(self) -> None:
        status, payload = self.get_json("/api/workflows/wf-unknown")
        self.assert_json_error(status, payload, 404, "WORKFLOW_NOT_FOUND")

    def test_workflow_save(self) -> None:
        status, payload = self.post_json(
            "/api/workflows", {"id": "wf-x", "name": "新工作流", "nodes": [], "edges": []}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["saved"])
        self.assertEqual(payload["workflow"]["id"], "wf-x")

    def test_tools_include_mcp_servers(self) -> None:
        status, payload = self.get_json("/api/tools")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["tools"]), 2)
        self.assertEqual(payload["tools"][0]["name"], "dataset_stats")
        self.assertEqual(payload["mcp_servers"][0]["name"], "weather-mcp")
        self.assertEqual(payload["mcp_servers"][0]["status"], "connected")


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
class RunTests(ServerTestCase):
    def test_start_run_and_detail(self) -> None:
        status, payload = self.post_json(
            "/api/runs",
            {"workflow_id": WORKFLOW_ID, "input": {"question": "归因分析"}, "policy": "adaptive"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["run_id"], RUN_ID)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(len(self.platform.start_requests), 1)
        request = self.platform.start_requests[0]
        self.assertIsInstance(request, RunRequest)
        self.assertEqual(request.workflow_id, WORKFLOW_ID)
        self.assertEqual(request.inputs, {"question": "归因分析"})
        self.assertEqual(request.policy, "adaptive")

        status, detail = self.get_json(f"/api/runs/{RUN_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["run_id"], RUN_ID)
        self.assertEqual(detail["status"], "succeeded")
        self.assertIn("nodes", detail)
        self.assertEqual(detail["nodes"]["analyze"]["confidence"], 0.93)

    def test_start_run_requires_workflow(self) -> None:
        status, payload = self.post_json("/api/runs", {"policy": "adaptive"})
        self.assert_json_error(status, payload, 400, "INVALID_RUN")

    def test_demo_run_uses_scenario(self) -> None:
        status, payload = self.post_json("/api/demo/run", {"policy": "adaptive"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["scenario"], "ai4s-air-quality")
        self.assertEqual(payload["run_id"], RUN_ID)
        request = self.platform.start_requests[0]
        self.assertEqual(request.scenario, "ai4s-air-quality")
        self.assertEqual(request.workflow_id, WORKFLOW_ID)
        self.assertEqual(request.policy, "adaptive")

    def test_list_runs(self) -> None:
        status, payload = self.get_json("/api/runs?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["runs"][0]["run_id"], RUN_ID)

    def test_run_not_found(self) -> None:
        status, payload = self.get_json("/api/runs/run-missing")
        self.assert_json_error(status, payload, 404, "RUN_NOT_FOUND")

    def test_cancel_run(self) -> None:
        status, payload = self.post_json(f"/api/runs/{RUN_ID}/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(self.platform.cancelled, [RUN_ID])

    def test_verification(self) -> None:
        status, payload = self.get_json(f"/api/runs/{RUN_ID}/verification")
        self.assertEqual(status, 200)
        self.assertEqual(payload["confidence"], 0.93)
        self.assertEqual(len(payload["layers"]), 5)
        self.assertGreaterEqual(len(payload["checkers"]), 4)
        self.assertIn("self_loop", payload)

    def test_loop_status(self) -> None:
        status, payload = self.get_json(f"/api/runs/{RUN_ID}/loop_status")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["self_loop"]["iterations"]), 2)

    def test_report_missing(self) -> None:
        status, payload = self.get_json(f"/api/runs/{RUN_ID}/report")
        self.assert_json_error(status, payload, 404, "REPORT_NOT_FOUND")

    def test_artifacts_list_and_download(self) -> None:
        status, payload = self.get_json(f"/api/runs/{RUN_ID}/artifacts")
        self.assertEqual(status, 200)
        names = [item["name"] for item in payload["artifacts"]]
        self.assertIn("report.md", names)

        status, headers, data = self.request("GET", f"/api/runs/{RUN_ID}/artifacts/report.md")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["content-type"])
        self.assertIn("归因报告".encode("utf-8"), data)

    def test_artifact_traversal_blocked(self) -> None:
        status, payload = self.get_json(f"/api/runs/{RUN_ID}/artifacts/..%2Fsecret.txt")
        self.assertIn(status, (400, 404))
        self.assertIn("error", payload)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
class SseTests(ServerTestCase):
    def _open_stream(self, path: str, headers: dict[str, str] | None = None) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = {"Accept": "text/event-stream"}
        if headers:
            request_headers.update(headers)
        conn.request("GET", path, headers=request_headers)
        return conn, conn.getresponse()

    @staticmethod
    def _read_frame(response: http.client.HTTPResponse, timeout: float = 10.0) -> list[str]:
        """读取一个 SSE 帧（以空行结束）的原始行。"""
        deadline = time.time() + timeout
        lines: list[str] = []
        while time.time() < deadline:
            line = response.fp.readline()  # type: ignore[union-attr]
            if not line:
                break
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if lines:
                    return lines
                continue
            lines.append(text)
        return lines

    def test_sse_replay_and_finish(self) -> None:
        conn, response = self._open_stream(f"/api/runs/{RUN_ID}/events")
        try:
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type", ""))
            frames: list[list[str]] = []
            for _ in range(20):
                lines = self._read_frame(response)
                if not lines:
                    break
                frames.append(lines)
                if any(line.startswith("event: run.finished") for line in lines):
                    break
            types = [
                line.split(": ", 1)[1]
                for frame in frames
                for line in frame
                if line.startswith("event: ")
            ]
            self.assertEqual(types[0], "run.started")
            self.assertIn("run.finished", types)
            ids = [
                int(line.split(": ", 1)[1])
                for frame in frames
                for line in frame
                if line.startswith("id: ")
            ]
            self.assertEqual(ids, sorted(ids))
            self.assertIn(1, ids)
            self.assertIn(5, ids)
            # data 行必须是合法 JSON 且带 seq / type
            for frame in frames:
                for line in frame:
                    if line.startswith("data: "):
                        event = json.loads(line[len("data: "):])
                        self.assertIn("seq", event)
                        self.assertIn("type", event)
        finally:
            conn.close()
        for _ in range(100):
            if self.platform.last_subscription is not None and self.platform.last_subscription.closed:
                break
            time.sleep(0.02)
        self.assertTrue(self.platform.last_subscription is None or self.platform.last_subscription.closed)

    def test_sse_respects_last_event_id(self) -> None:
        conn, response = self._open_stream(
            f"/api/runs/{RUN_ID}/events", headers={"Last-Event-ID": "4"}
        )
        try:
            self.assertEqual(response.status, 200)
            text_parts: list[str] = []
            for _ in range(10):
                lines = self._read_frame(response)
                if not lines:
                    break
                text_parts.extend(lines)
                if any(line.startswith("event: run.finished") for line in lines):
                    break
            text = "\n".join(text_parts)
            self.assertIn("id: 5", text)
            self.assertNotIn("id: 1", text)
            self.assertNotIn("event: run.started", text)
        finally:
            conn.close()

    def test_sse_unknown_run(self) -> None:
        status, payload = self.get_json("/api/runs/run-missing/events")
        self.assert_json_error(status, payload, 404, "RUN_NOT_FOUND")


class HeartbeatTests(ServerTestCase):
    platform_class = HeartbeatPlatform
    config_overrides = {"sse_heartbeat_s": 0.2}

    def test_heartbeat_and_non_blocking(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/runs/{RUN_ID}/events", headers={"Accept": "text/event-stream"})
        response = conn.getresponse()
        try:
            self.assertEqual(response.status, 200)
            ping_seen = False
            deadline = time.time() + 5
            while time.time() < deadline:
                line = response.fp.readline()  # type: ignore[union-attr]
                if not line:
                    break
                if line.decode("utf-8").strip() == ": ping":
                    ping_seen = True
                    break
            self.assertTrue(ping_seen, "未收到 SSE 心跳注释")
        finally:
            # SSE 连接保持期间，普通 REST 必须立即返回（ThreadingHTTPServer 不阻塞）
            started = time.time()
            status, payload = self.get_json("/api/health")
            elapsed = time.time() - started
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertLess(elapsed, 2.0, "SSE 长连接阻塞了 REST 请求")
            conn.close()


class LegacySubscribeSseTests(SseTests):
    """真实门面 ``subscribe(run_id)`` 签名下 SSE 仍需正常回放历史并正常收尾。"""

    platform_class = LegacySubscribePlatform


# ---------------------------------------------------------------------------
# 模型设置 / 静态文件 / 错误处理
# ---------------------------------------------------------------------------
class LlmConfigTests(ServerTestCase):
    def test_get_config_masks_key(self) -> None:
        status, payload = self.get_json("/api/config/llm")
        self.assertEqual(status, 200)
        self.assertEqual(payload["llm"]["provider"], "mock")
        self.assertEqual(payload["llm"]["api_key"], "sk-t****abcd")
        self.assertNotIn("1234567890", json.dumps(payload, ensure_ascii=False))

    def test_post_config_switch_provider(self) -> None:
        status, payload = self.post_json(
            "/api/settings/llm",
            {
                "provider": "openai_compat",
                "base_url": "https://example.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-new-secret-key",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["llm"]["provider"], "openai_compat")
        self.assertEqual(payload["llm"]["api_key"], "sk-n****-key")
        self.assertEqual(len(self.platform.llm_payloads), 1)
        # 平台收到的是完整 Key（只有响应打码）
        self.assertEqual(self.platform.llm_payloads[0]["api_key"], "sk-new-secret-key")

    def test_invalid_provider(self) -> None:
        status, payload = self.post_json("/api/settings/llm", {"provider": "unknown-llm"})
        self.assert_json_error(status, payload, 400, "INVALID_PROVIDER")


class StaticAndErrorTests(ServerTestCase):
    def test_index_served(self) -> None:
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn("织流 AgentWeave".encode("utf-8"), data)

    def test_js_mime(self) -> None:
        status, headers, data = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        self.assertIn("织流前端".encode("utf-8"), data)

    def test_head_request(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("HEAD", "/")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.getheader("Content-Length") or 0), 0)
            self.assertEqual(response.read(), b"")
        finally:
            conn.close()

    def test_path_traversal_blocked(self) -> None:
        for path in ("/../config/platform.json", "/%2e%2e/config/platform.json"):
            status, payload = self.get_json(path)
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_unknown_api(self) -> None:
        status, payload = self.get_json("/api/does-not-exist")
        self.assert_json_error(status, payload, 404, "NOT_FOUND")

    def test_method_not_allowed(self) -> None:
        status, payload = self.post_json("/api/health", {})
        self.assert_json_error(status, payload, 405, "METHOD_NOT_ALLOWED")

    def test_invalid_json_body(self) -> None:
        status, payload = self.post_json(
            "/api/runs", None, raw_body=b"{not json"
        )
        self.assert_json_error(status, payload, 400, "INVALID_JSON")

    def test_non_object_body(self) -> None:
        status, payload = self.post_json("/api/runs", None, raw_body=b"[1, 2, 3]")
        self.assert_json_error(status, payload, 400, "INVALID_BODY")

    def test_body_too_large(self) -> None:
        status, headers, data = self.request(
            "POST",
            "/api/runs",
            headers={"Content-Type": "application/json", "Content-Length": str(3 * 1024 * 1024)},
            raw_body=b"{}",
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(data.decode("utf-8"))["error"]["code"], "BODY_TOO_LARGE")

    def test_stats_timeseries(self) -> None:
        status, payload = self.get_json("/api/stats/timeseries")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["series"]), 1)
        self.assertEqual(payload["series"][0]["run_id"], RUN_ID)


# ---------------------------------------------------------------------------
# 2026-11-16 回归用例
# ---------------------------------------------------------------------------
TRAILING_LOG_EVENT: dict[str, Any] = {
    "seq": 6,
    "type": "log",
    "run_id": RUN_ID,
    "node_id": None,
    "agent_id": None,
    "payload": {"message": "运行结束：succeeded（耗时 0.916s，Token 28651，自循环 2 轮）"},
    "level": "info",
    "ts": NOW - 16,
}


class TrailingLogPlatform(FakePlatform):
    """复现真实事件顺序：``run.finished`` 之后还有 log 事件。

    修复前 SSE 回放段用 ``finished = data["finished"]`` 逐个覆盖，末尾的 log
    会把 finished 重置为 False，连接永不关闭。
    """

    def get_events(self, run_id: str, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        stream = list(EVENTS) + [TRAILING_LOG_EVENT]
        return [event for event in stream if int(event.get("seq") or 0) > since_seq][:limit]

    def subscribe(self, run_id: str, last_event_id: int = 0) -> FakeSubscription:
        self.last_subscription = FakeSubscription([])
        return self.last_subscription


class SseFinishedReplayTests(ServerTestCase):
    """运行已结束后再订阅 SSE：回放完历史必须主动关闭连接。"""

    platform_class = TrailingLogPlatform

    def test_replay_closes_after_finished_with_trailing_log(self) -> None:
        started = time.time()
        status, headers, data = self.request(
            "GET", f"/api/runs/{RUN_ID}/events", timeout=5.0
        )
        elapsed = time.time() - started
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        text = data.decode("utf-8")
        self.assertIn("event: run.finished", text)
        self.assertIn("event: log", text)
        self.assertLess(elapsed, 4.0, "运行结束后 SSE 回放未关闭（finished 被后续事件覆盖）")

    def test_trailing_log_keeps_finished_state(self) -> None:
        _status, _headers, data = self.request(
            "GET", f"/api/runs/{RUN_ID}/events", timeout=5.0
        )
        ids = [
            int(line.split(": ", 1)[1])
            for line in data.decode("utf-8").splitlines()
            if line.startswith("id: ")
        ]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids[-1], TRAILING_LOG_EVENT["seq"])


class PolicyValidationTests(ServerTestCase):
    """非法调策略必须在 API 层 400，且不能把脏值传给平台。"""

    def test_invalid_policy_returns_400(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"workflow_id": WORKFLOW_ID, "policy": "nonsense"}
        )
        self.assert_json_error(status, payload, 400, "INVALID_POLICY")
        self.assertEqual(self.platform.start_requests, [], "脏策略不应到达平台层")

    def test_policy_alias_normalized_before_platform(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"workflow_id": WORKFLOW_ID, "policy": "rr"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.platform.start_requests[-1].policy, "round_robin")


class FailingStartPlatform(FakePlatform):
    """start_run 抛平台内部错误，用于验证 API 层错误码映射。"""

    start_error = "平台内部错误（start_run）：工作流不存在：wf-nope"

    def start_run(self, request: RunRequest) -> str:  # type: ignore[override]
        raise ApiError(500, "PLATFORM_ERROR", self.start_error)


class UnknownWorkflowMappingTests(ServerTestCase):
    platform_class = FailingStartPlatform

    def test_unknown_workflow_maps_to_404(self) -> None:
        status, payload = self.post_json("/api/runs", {"workflow_id": "wf-nope"})
        self.assert_json_error(status, payload, 404, "WORKFLOW_NOT_FOUND")


class ConcurrencyStartPlatform(FailingStartPlatform):
    start_error = "平台内部错误（start_run）：并发运行数已达上限（4），请稍后再试"


class ConcurrencyMappingTests(ServerTestCase):
    platform_class = ConcurrencyStartPlatform

    def test_concurrency_maps_to_429(self) -> None:
        status, payload = self.post_json("/api/runs", {"workflow_id": WORKFLOW_ID})
        self.assert_json_error(status, payload, 429, "TOO_MANY_RUNS")


class PolicyErrorStartPlatform(FailingStartPlatform):
    start_error = "平台内部错误（start_run）：未知调度策略: 'x'"


class PolicyErrorMappingTests(ServerTestCase):
    """平台层二次校验兜底：即使 API 层校验被绕过，也要映射成 400。"""

    platform_class = PolicyErrorStartPlatform

    def test_policy_error_maps_to_400(self) -> None:
        status, payload = self.post_json("/api/runs", {"workflow_id": WORKFLOW_ID})
        self.assert_json_error(status, payload, 400, "INVALID_POLICY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
