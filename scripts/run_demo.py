"""命令行运行演示场景：一次完整的「编排 → 执行 → 验证 → 自循环 → 报告」。

用法::

    py -3.14 scripts/run_demo.py                # 跑 AI4S 演示场景
    py -3.14 scripts/run_demo.py --json         # 机器可读输出（给 CI / 评测用）
    py -3.14 scripts/run_demo.py --timeout 300   # 调整超时（秒）

退出码：0 = 运行成功且验证通过；1 = 运行失败/验证未通过；2 = 环境或参数错误。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import RunRequest  # noqa: E402
from agentweave.demo import scenario as demo  # noqa: E402
from agentweave.runtime.platform import create_platform, load_platform_config  # noqa: E402


def build_request(label: str) -> RunRequest:
    payload = demo.run_request_payload(label)
    return RunRequest(
        workflow_id=payload["workflow_id"],
        inputs=payload["inputs"],
        policy=payload["policy"],
        label=payload["label"],
        scenario=payload["scenario"],
        auto_approve=payload["auto_approve"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行织流 AgentWeave 演示场景")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument("--timeout", type=float, default=240.0, help="等待运行的超时秒数")
    parser.add_argument("--label", default="CLI 演示运行", help="运行标签")
    args = parser.parse_args(argv)

    config = load_platform_config()
    platform = create_platform(config)
    started = time.time()
    try:
        platform.start()
        request = build_request(args.label)
        run_id = platform.start_run(request)
        if not args.json:
            print(f"[1/4] 已提交运行 {run_id}（工作流 {request.workflow_id}）")
        summary = platform.wait_run(run_id, timeout=args.timeout)
        if summary is None:
            print(f"运行超时（>{args.timeout}s）：{run_id}", file=sys.stderr)
            return 1
        run = platform.get_run(run_id) or {}
        verification = platform.get_verification(run_id)
        loop = platform.get_loop_status(run_id)
        report = platform.get_report(run_id) or {}
        metrics = run.get("metrics") or {}
        status = str(run.get("status") or "").lower()
        passed = any(bool(item.get("passed")) for item in verification) if verification else False

        if not args.json:
            print(f"[2/4] 运行结束：{status}（耗时 {time.time() - started:.1f}s）")
            print(
                f"      节点 {metrics.get('nodes_total', 0)} 个，成功 {metrics.get('succeeded', 0)} 个，"
                f"Token {metrics.get('tokens_total', 0)}"
            )
            print(
                f"[3/4] 验证 {len(verification)} 次，自循环 {loop.get('iterations', 0)} 轮，"
                f"最终通过：{passed}"
            )
            report_path = report.get("path") or ""
            if report_path:
                print(f"[4/4] 报告已生成：{report_path}")
            print("\n—— 运行报告预览 ——\n")
            print((report.get("markdown") or "").strip()[:1800])

        payload = {
            "ok": status == "succeeded" and passed,
            "run_id": run_id,
            "status": status,
            "error": run.get("error", ""),
            "metrics": metrics,
            "verification_count": len(verification),
            "verification_passed": passed,
            "self_loop": loop,
            "report_path": report.get("path", ""),
            "duration_s": round(time.time() - started, 2),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    finally:
        platform.close()


if __name__ == "__main__":
    raise SystemExit(main())
