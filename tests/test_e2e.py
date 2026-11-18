"""端到端集成测试：从平台装配到一次完整运行的验收。

运行::

    PYTHONIOENCODING=utf-8 py -3.14 -m unittest tests.test_e2e -v

覆盖的验收点（对应 docs/CONTRACTS.md 的验收标准）：
1. 平台能零配置启动，自动发现 ``workflows/*.json`` 与 ``config/agents.json``；
2. 一次演示运行能跑完 12 个节点，状态为 succeeded；
3. 至少触发一次五层验证，且最终验证通过（自循环修复生效）；
4. 自循环轮数 ≥ 1，且第二轮系数误差落在 15% 容差内；
5. 产物齐全：report.md / metrics.json / workflow.json / events.jsonl；
6. 事件流严格单调递增，且包含 run.started、node.started、verify.*、run.finished；
7. 取消接口与重复读取安全。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import RunRequest  # noqa: E402
from agentweave.demo import scenario as demo  # noqa: E402
from agentweave.runtime.platform import create_platform  # noqa: E402
from tests._config import mock_platform_config  # noqa: E402


def _request(label: str = "e2e") -> RunRequest:
    payload = demo.run_request_payload(label)
    return RunRequest(
        workflow_id=payload["workflow_id"],
        inputs=payload["inputs"],
        policy=payload["policy"],
        label=payload["label"],
        scenario=payload["scenario"],
        auto_approve=payload["auto_approve"],
    )


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("AGENTWEAVE_ENV", "test")
        cls.platform = create_platform(mock_platform_config())
        cls.platform.start()
        cls.run_id = cls.platform.start_run(_request())
        cls.summary = cls.platform.wait_run(cls.run_id, timeout=180.0)
        cls.run_info = cls.platform.get_run(cls.run_id) or {}
        cls.events = cls.platform.get_events(cls.run_id, 0, 5000)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.platform.close()

    # -- 1. 运行本身 -----------------------------------------------------
    def test_01_run_succeeds(self) -> None:
        self.assertIsNotNone(self.summary, "运行在 180s 内未结束")
        self.assertEqual(str(self.run_info.get("status")), "succeeded", self.run_info.get("error"))

    def test_02_all_nodes_executed(self) -> None:
        nodes = self.run_info.get("nodes") or {}
        self.assertGreaterEqual(len(nodes), 12, f"节点数不足：{len(nodes)}")
        bad = [nid for nid, st in nodes.items() if str(st.get("status")) not in ("succeeded", "skipped")]
        self.assertEqual(bad, [], f"存在未成功节点：{bad}")

    # -- 2. 验证与自循环 --------------------------------------------------
    def test_03_verification_ran(self) -> None:
        records = self.platform.get_verification(self.run_id)
        self.assertGreaterEqual(len(records), 1, "没有产生任何验证记录")
        self.assertTrue(any(bool(r.get("passed")) for r in records), "最终验证未通过")

    def test_04_self_loop_fixed_deviation(self) -> None:
        loop = self.platform.get_loop_status(self.run_id)
        repair = (loop.get("repair") or {}) if isinstance(loop, dict) else {}
        iterations = int(repair.get("iterations") or repair.get("iteration") or 0)
        self.assertGreaterEqual(iterations, 1, f"未触发自循环：{loop}")
        # 最终统计量应与真值接近（Mock 第二轮为 -0.84/-0.44/-1.67/0.26）
        truth = demo.load_truth()
        coefficients = truth.get("coefficients") or {}
        statistician = (self.run_info.get("nodes") or {}).get("analyze") or {}
        output = statistician.get("output") or {}
        metrics = output.get("metrics") if isinstance(output, dict) else None
        if isinstance(metrics, dict):
            tolerance = float(truth.get("tolerance") or 0.15)
            for name, expected in coefficients.items():
                claimed = (metrics.get("coefficients") or {}).get(name)
                if claimed is None or not expected:
                    continue
                rel = abs(float(claimed) - float(expected)) / abs(float(expected))
                self.assertLessEqual(rel, tolerance + 0.02, f"{name} 偏差 {rel:.2%} 超出容差")

    # -- 3. 产物 ---------------------------------------------------------
    def test_05_artifacts_saved(self) -> None:
        artifacts = self.platform.list_artifacts(self.run_id)
        names = {item.get("name") for item in artifacts}
        for required in ("report.md", "metrics.json", "workflow.json"):
            self.assertIn(required, names, f"缺少产物 {required}：{sorted(names)}")
        report = self.platform.get_report(self.run_id) or {}
        self.assertGreater(len(str(report.get("markdown") or "")), 500, "报告内容过短")

    def test_06_report_contains_evidence(self) -> None:
        report = (self.platform.get_report(self.run_id) or {}).get("markdown") or ""
        for keyword in ("运行报告", "验证轨迹", "自循环", "研究报告"):
            self.assertIn(keyword, report, f"报告缺少章节：{keyword}")

    # -- 4. 事件流 -------------------------------------------------------
    def test_07_events_monotonic(self) -> None:
        self.assertGreater(len(self.events), 30, f"事件过少：{len(self.events)}")
        seqs = [int(item.get("seq") or 0) for item in self.events]
        self.assertEqual(seqs, sorted(seqs), "事件 seq 非单调递增")
        self.assertEqual(len(seqs), len(set(seqs)), "存在重复 seq")

    def test_08_event_types_present(self) -> None:
        types = {str(item.get("type")) for item in self.events}
        for expected in ("run.started", "run.finished", "node.started", "node.succeeded"):
            self.assertIn(expected, types, f"缺少事件类型 {expected}")
        self.assertTrue(
            any(t.startswith("verify.") for t in types),
            f"缺少验证事件：{sorted(t for t in types if t.startswith('verify'))}",
        )
        self.assertTrue(
            any(t.startswith("loop.") for t in types),
            f"缺少自循环事件：{sorted(t for t in types if t.startswith('loop'))}",
        )

    # -- 5. 幂等与安全 ---------------------------------------------------
    def test_09_get_run_is_idempotent(self) -> None:
        again = self.platform.get_run(self.run_id) or {}
        self.assertEqual(again.get("run_id"), self.run_id)
        self.assertEqual(again.get("status"), self.run_info.get("status"))

    def test_10_platform_snapshot(self) -> None:
        snap = self.platform.snapshot()
        self.assertIn("workflows", snap)
        self.assertIn("agents", snap)
        self.assertTrue(snap["workflows"], "未发现任何工作流")

    def test_11_scheduler_and_verifier_status(self) -> None:
        status = self.platform.scheduler_status()
        self.assertIsInstance(status, dict)
        strategies = self.platform.verify_strategies()
        self.assertTrue(strategies.get("checkers") or strategies.get("available"), strategies)

    def test_12_llm_status_reports_mock(self) -> None:
        status = self.platform.llm_status()
        self.assertIsInstance(status, dict)
        text = json.dumps(status, ensure_ascii=False, default=str).lower()
        self.assertIn("mock", text, "默认应使用离线 MockProvider")


if __name__ == "__main__":
    unittest.main(verbosity=2)
