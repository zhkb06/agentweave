"""回归测试：覆盖 2026-11-16 排查出的 7 个缺陷的修复。

对应缺陷与断言：

1. SSE 回放永不关闭 → ``tests/test_api.py::SseFinishedReplayTests``（HTTP 层）。
2. 产物下载 100% 404 → 本文件 ``test_artifact_path_resolves_existing_file``。
3. 取消接口 500（``EventType.RUN_CANCEL_REQUESTED`` 不存在）→
   本文件 ``test_cancel_run_emits_event``。
4. 并发上限对突发请求失效（pending 未计入）→
   ``ConcurrencyLimitTests.test_pending_run_counts_towards_limit``。
5. 非法 policy 被静默接受 → 本文件 ``test_invalid_policy_rejected``
   （平台层二次校验）与 ``tests/test_api.py::PolicyValidationTests``（API 400）。
6. 未知工作流 500 → ``tests/test_api.py::UnknownWorkflowMappingTests``（404）。
7. ``EventType.RUN_ERROR`` 不存在被吞 → 常量化后由静态扫描覆盖，本文件顺带
   断言事件常量存在，防止再次出现「引用不存在的常量」。

另外验证 per-run 策略真正生效、调度统计跨运行可累积（adaptive 不再永远 0.5）。

运行::

    PYTHONIOENCODING=utf-8 py -3.14 -m unittest tests.test_regressions -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import EventType, RunRequest  # noqa: E402
from agentweave.demo import scenario as demo  # noqa: E402
from agentweave.runtime.platform import create_platform  # noqa: E402
from tests._config import mock_platform_config  # noqa: E402


def _request(label: str = "regression", policy: str = "adaptive") -> RunRequest:
    payload = demo.run_request_payload(label)
    return RunRequest(
        workflow_id=payload["workflow_id"],
        inputs=payload["inputs"],
        policy=policy,
        label=payload["label"],
        scenario=payload["scenario"],
        auto_approve=payload["auto_approve"],
    )


class PlatformRegressionTests(unittest.TestCase):
    """真实平台（离线 MockProvider）上验证修复后的门面行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("AGENTWEAVE_ENV", "test")
        cls.platform = create_platform(mock_platform_config())
        cls.platform.start()
        cls.run_id = cls.platform.start_run(_request())
        cls.platform.wait_run(cls.run_id, timeout=180.0)
        cls.run_info = cls.platform.get_run(cls.run_id) or {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.platform.close()

    # -- 缺陷 2：产物路径 -------------------------------------------------
    def test_artifact_path_resolves_existing_file(self) -> None:
        path = Path(self.platform.artifact_path(self.run_id, "report.md"))
        self.assertTrue(path.is_file(), f"产物路径不可用：{path}")
        self.assertEqual(path.name, "report.md")
        self.assertIn(self.run_id, str(path))

    # -- 缺陷 5：非法策略在平台层也必须被拒绝 -----------------------------
    def test_invalid_policy_rejected(self) -> None:
        for bad in ("nonsense", "adaptive-x", "random"):
            with self.assertRaises(ValueError, msg=bad):
                self.platform.start_run(_request(policy=bad))

    # -- per-run 策略真正生效（此前只是记录字段）--------------------------
    def test_per_run_policy_takes_effect(self) -> None:
        run_id = self.platform.start_run(_request("policy-alias", policy="rr"))
        self.platform.wait_run(run_id, timeout=180.0)
        info = self.platform.get_run(run_id) or {}
        self.assertEqual(str(info.get("policy")), "round_robin")

    # -- 缺陷 3：取消运行必须产生 run.cancel_requested 事件 ----------------
    def test_cancel_run_emits_event(self) -> None:
        names = dir(EventType)
        self.assertIn("RUN_CANCEL_REQUESTED", names)
        self.assertIn("RUN_ERROR", names)
        self.assertEqual(EventType.RUN_CANCEL_REQUESTED, "run.cancel_requested")
        self.assertEqual(EventType.RUN_ERROR, "run.error")
        run_id = self.platform.start_run(_request("cancel-me", policy="capability"))
        accepted = self.platform.cancel_run(run_id)
        info = self.platform.wait_run(run_id, timeout=180.0) or {}
        self.assertIn(
            str(info.get("status")), ("cancelled", "succeeded"), f"取消后状态异常：{info}"
        )
        if accepted:
            types = {str(event.get("type")) for event in self.platform.get_events(run_id, 0, 5000)}
            self.assertIn("run.cancel_requested", types)

    # -- 调度统计跨运行累积（此前 record_outcome 全仓无人调用）------------
    def test_scheduler_stats_accumulate(self) -> None:
        status = self.platform.scheduler_status()
        self.assertGreater(int(status.get("decisions") or 0), 0, status)
        self.assertGreaterEqual(len(status.get("agent_success") or {}), 1, status)
        self.assertNotEqual(status.get("global_success_rate"), 0.5, status)

    # -- 显式未知工作流不得静默回退到 scenario ----------------------------
    def test_unknown_workflow_does_not_fall_back_to_scenario(self) -> None:
        payload = demo.run_request_payload("regression-unknown-workflow")
        request = RunRequest(
            workflow_id="wf-does-not-exist",
            inputs=payload["inputs"],
            policy="adaptive",
            label="regression-unknown-workflow",
            scenario="ai4s-air-quality",
            auto_approve=True,
        )
        with self.assertRaises(ValueError) as ctx:
            self.platform.start_run(request)
        self.assertIn("工作流不存在", str(ctx.exception))

    # -- 只给 scenario（不指定 workflow_id）仍应正常解析 ------------------
    def test_scenario_only_still_resolves(self) -> None:
        payload = demo.run_request_payload("regression-scenario-only")
        request = RunRequest(
            workflow_id="",
            inputs=payload["inputs"],
            policy="adaptive",
            label="regression-scenario-only",
            scenario="ai4s-air-quality",
            auto_approve=True,
        )
        run_id = self.platform.start_run(request)
        self.assertTrue(run_id)
        self.platform.wait_run(run_id, timeout=180.0)
        info = self.platform.get_run(run_id) or {}
        self.assertEqual(str(info.get("workflow_id")), "wf-ai4s-air-quality")


class ConcurrencyLimitTests(unittest.TestCase):
    """缺陷 4：新建 run 处于 pending 时也必须计入并发上限。"""

    def setUp(self) -> None:
        cfg = mock_platform_config()
        runtime = dict(cfg.get("runtime") or {})
        runtime["max_concurrent_runs"] = 1
        cfg["runtime"] = runtime
        self.platform = create_platform(cfg)
        self.platform.start()
        self.addCleanup(self.platform.close)

    def test_pending_run_counts_towards_limit(self) -> None:
        first = self.platform.start_run(_request("first", policy="adaptive"))
        with self.assertRaises(RuntimeError) as ctx:
            self.platform.start_run(_request("second", policy="adaptive"))
        self.assertIn("并发", str(ctx.exception))
        self.platform.cancel_run(first)
        self.platform.wait_run(first, timeout=60.0)


class ConcurrentAdmissionTests(unittest.TestCase):
    """缺陷 4 的并发面：突发提交时「计数 + 创建」必须原子，不能被竞态穿透。"""

    def setUp(self) -> None:
        cfg = mock_platform_config()
        runtime = dict(cfg.get("runtime") or {})
        runtime["max_concurrent_runs"] = 2
        cfg["runtime"] = runtime
        self.platform = create_platform(cfg)
        self.platform.start()
        self.addCleanup(self.platform.close)

    def test_burst_never_exceeds_limit(self) -> None:
        accepted: list[str] = []
        rejected: list[str] = []
        lock = threading.Lock()

        def fire(index: int) -> None:
            try:
                run_id = self.platform.start_run(_request(f"burst-{index}", policy="adaptive"))
            except RuntimeError as exc:
                with lock:
                    rejected.append(str(exc))
                return
            with lock:
                accepted.append(run_id)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(accepted), 2, f"上限 2 被穿透：放行 {len(accepted)} 个")
        self.assertEqual(len(rejected), 6)
        self.assertTrue(all("并发" in message for message in rejected))
        for run_id in accepted:
            self.platform.cancel_run(run_id)
            self.platform.wait_run(run_id, timeout=60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
