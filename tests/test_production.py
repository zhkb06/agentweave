"""生产化加固测试：鉴权、限流、指标、安全头、运行历史持久化。

运行::

    PYTHONIOENCODING=utf-8 py -3.14 -m unittest tests.test_production -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import RunRequest  # noqa: E402
from agentweave.runtime.platform import Platform  # noqa: E402
from tests._config import mock_platform_config  # noqa: E402
from tests.test_api import ServerTestCase  # noqa: E402

TOKEN = "test-token-0123456789abcdef"


class AuthTests(ServerTestCase):
    """启用 auth_token 后：写接口 401，读接口与 health 保持可用。"""

    config_overrides = {"auth_token": TOKEN}

    def test_post_without_token_rejected(self) -> None:
        status, payload = self.post_json("/api/demo/run", {"label": "auth"})
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "UNAUTHORIZED")

    def test_post_with_bearer_token_ok(self) -> None:
        status, payload = self.post_json(
            "/api/demo/run", {"label": "auth"}, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("run_id", payload)

    def test_post_with_query_token_ok(self) -> None:
        status, payload = self.post_json(f"/api/demo/run?token={TOKEN}", {"label": "auth"})
        self.assertEqual(status, 200, payload)

    def test_wrong_token_rejected(self) -> None:
        status, payload = self.post_json(
            "/api/demo/run", {"label": "auth"}, headers={"Authorization": "Bearer wrong-token"}
        )
        self.assertEqual(status, 401, payload)

    def test_health_and_reads_open(self) -> None:
        status, _ = self.get_json("/api/health")
        self.assertEqual(status, 200)
        status, _ = self.get_json("/api/runs")
        self.assertEqual(status, 200)


class RateLimitTests(ServerTestCase):
    """每 IP 运行接口限流：超过 run_per_minute 返回 429。"""

    config_overrides = {"limits": {"run_per_minute": 2}}

    def test_run_rate_limited(self) -> None:
        for index in range(2):
            status, payload = self.post_json("/api/demo/run", {"label": f"rate-{index}"})
            self.assertEqual(status, 200, payload)
        status, payload = self.post_json("/api/demo/run", {"label": "rate-over"})
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload["error"]["code"], "RATE_LIMITED")


class MetricsTests(ServerTestCase):
    def test_metrics_endpoint(self) -> None:
        self.get_json("/api/health")
        status, payload = self.get_json("/api/metrics")
        self.assertEqual(status, 200, payload)
        self.assertIn("requests", payload)
        self.assertGreaterEqual(payload["requests"]["total"], 1)
        self.assertIn("runs", payload)
        self.assertIn("platform", payload)
        self.assertFalse(payload["auth_enabled"])

    def test_security_headers(self) -> None:
        status, headers, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")


class PersistenceTests(unittest.TestCase):
    """运行结束后写 summary.json；新进程可通过磁盘摘要查询历史。"""

    def test_summary_survives_restart(self) -> None:
        config = mock_platform_config()
        config["runtime"] = dict(config.get("runtime") or {})
        config["runtime"]["run_history_keep"] = 0  # 测试不清理既有目录
        first = Platform(config, root=ROOT)
        second = None
        try:
            first.start()
            request = RunRequest(
                workflow_id="wf-ai4s-air-quality",
                scenario="ai4s-air-quality",
                label="生产化持久化测试",
                auto_approve=True,
            )
            run_id = first.start_run(request)
            result = first.wait_run(run_id, timeout=90)
            self.assertIsNotNone(result)
            self.assertEqual(result.get("status"), "succeeded", result)
            summary_path = ROOT / "data" / "runs" / run_id / "summary.json"
            self.assertTrue(summary_path.is_file(), "summary.json 未落盘")
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(saved.get("run_id"), run_id)

            # 模拟重启：新平台实例内存中没有该 run，应回落到磁盘摘要
            second = Platform(config, root=ROOT)
            detail = second.get_run(run_id)
            self.assertIsNotNone(detail)
            self.assertEqual(detail.get("run_id"), run_id)
            listed = second.list_runs(50)
            self.assertIn(run_id, {item.get("run_id") for item in listed})
        finally:
            if second is not None:
                second.close()
            first.close()


class CleanupTests(unittest.TestCase):
    def test_cleanup_keeps_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "data" / "runs"
            for index in range(4):
                directory = runs / f"run-old-{index}"
                directory.mkdir(parents=True)
                (directory / "summary.json").write_text(
                    json.dumps({"run_id": directory.name, "created_at": index}), encoding="utf-8"
                )
            config = {"runtime": {"run_history_keep": 2}}
            platform = Platform(config, root=root)
            platform._cleanup_runs()
            remaining = sorted(item.name for item in runs.iterdir())
            self.assertEqual(len(remaining), 2)
            platform.close()


if __name__ == "__main__":
    unittest.main()
