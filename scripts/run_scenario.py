#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行任意场景演示：完整走一遍「编排 → 执行 → 验证失败 → 自循环修复 → 通过」。

用法::

    py -3.14 scripts/run_scenario.py --list
    py -3.14 scripts/run_scenario.py --scenario finance-sales-attribution
    py -3.14 scripts/run_scenario.py --scenario fin-sales --json
    py -3.14 scripts/run_scenario.py --all

退出码：0 = 所有运行成功且最终验证通过；1 = 失败；2 = 参数/环境错误。
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
from agentweave.runtime.platform import create_platform, load_platform_config  # noqa: E402

SCENARIOS_DIR = ROOT / "scenarios"


def discover() -> list[dict]:
    out: list[dict] = []
    if not SCENARIOS_DIR.is_dir():
        return out
    for path in sorted(SCENARIOS_DIR.glob("*/scenario.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["_dir"] = path.parent.name
        out.append(data)
    return out


def resolve(key: str, scenarios: list[dict]) -> dict | None:
    key = (key or "").strip()
    for item in scenarios:
        if key in (item.get("id"), item.get("short"), item.get("_dir")):
            return item
    return None


def run_one(platform, scenario: dict, timeout: float, as_json: bool) -> tuple[bool, dict]:
    name = scenario.get("name") or scenario.get("id")
    wf_id = scenario["workflow_id"]
    request = RunRequest(
        workflow_id=wf_id,
        inputs={"task": scenario.get("task", ""), "dataset": scenario.get("dataset", "")},
        label=f"场景演示：{name}",
        scenario=str(scenario.get("id") or ""),
        auto_approve=True,
    )
    started = time.time()
    run_id = platform.start_run(request)
    if not as_json:
        print(f"\n[{name}] 已提交运行 {run_id}（工作流 {wf_id}）")
    summary = platform.wait_run(run_id, timeout=timeout)
    if summary is None:
        return False, {"scenario": scenario.get("id"), "run_id": run_id, "ok": False,
                       "error": f"等待运行超时（>{timeout}s）"}
    run = platform.get_run(run_id) or {}
    verification = platform.get_verification(run_id) or []
    loop = platform.get_loop_status(run_id) or {}
    report = platform.get_report(run_id) or {}
    metrics = run.get("metrics") or {}
    status = str(run.get("status") or "").lower()
    passed = bool(verification) and bool(verification[-1].get("passed"))

    rounds = []
    for idx, rec in enumerate(verification, start=1):
        failures = [c.get("name") for c in (rec.get("checks") or []) if not c.get("passed")]
        rounds.append({
            "round": idx,
            "passed": bool(rec.get("passed")),
            "confidence": round(float(rec.get("confidence") or 0), 4),
            "failed_checks": failures,
        })

    loop_summ = (loop.get("self_loop") or {}).get("repair") if isinstance(loop.get("self_loop"), dict) else None
    if loop_summ is None:
        loop_summ = (loop.get("repair") if isinstance(loop.get("repair"), dict) else {}) or {}

    node_states = run.get("node_states") or {}
    analyze = (node_states.get("analyze") or {}).get("output") or {}
    claimed = ((analyze.get("metrics") or {}).get("coefficients") or {}) if isinstance(analyze, dict) else {}
    truth = {}
    truth_file = SCENARIOS_DIR / str(scenario.get("id")) / "truth.json"
    if truth_file.is_file():
        try:
            truth = json.loads(truth_file.read_text(encoding="utf-8")).get("coefficients") or {}
        except (OSError, ValueError):
            truth = {}

    payload = {
        "scenario": scenario.get("id"),
        "name": name,
        "run_id": run_id,
        "status": status,
        "ok": status == "succeeded" and passed,
        "rounds": rounds,
        "self_loop_iterations": int(metrics.get("self_loop_iterations") or loop_summ.get("iterations") or 0),
        "converged": bool(loop_summ.get("converged")),
        "tokens_total": metrics.get("tokens_total", 0),
        "duration_s": round(time.time() - started, 2),
        "claimed_coefficients": claimed,
        "truth_coefficients": truth,
        "report_path": report.get("path", ""),
    }
    if not as_json:
        print(f"[{name}] 状态 {status} · 耗时 {payload['duration_s']}s · Token {payload['tokens_total']}")
        for item in rounds:
            flag = "通过" if item["passed"] else "失败"
            extra = ("；失败项：" + "、".join(item["failed_checks"])) if item["failed_checks"] else ""
            print(f"  第 {item['round']} 轮验证：{flag} · 置信度 {item['confidence']:.3f}{extra}")
        print(f"  自循环：{payload['self_loop_iterations']} 轮 · converged={payload['converged']}")
        if payload["report_path"]:
            print(f"  报告：{payload['report_path']}")
    return bool(payload["ok"]), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 AgentWeave 多场景演示")
    parser.add_argument("--scenario", help="场景 id 或简称（见 --list）")
    parser.add_argument("--all", action="store_true", help="依次运行全部场景")
    parser.add_argument("--list", action="store_true", help="列出可用场景")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument("--timeout", type=float, default=240.0, help="单次运行超时（秒）")
    args = parser.parse_args(argv)

    scenarios = discover()
    if args.list:
        print("可用场景：")
        for item in scenarios:
            print(f"  {item.get('id'):<32} {item.get('short'):<12} {item.get('name')}（{item.get('workflow_id')}）")
        return 0
    if not scenarios:
        print("未找到场景：请先运行 py -3.14 scripts/generate_scenarios.py", file=sys.stderr)
        return 2

    if args.all:
        targets = scenarios
    else:
        target = resolve(args.scenario or "", scenarios)
        if target is None:
            print(f"未知场景：{args.scenario!r}；用 --list 查看可用场景", file=sys.stderr)
            return 2
        targets = [target]

    platform = create_platform(load_platform_config())
    results = []
    try:
        platform.start()
        missing = [item["workflow_id"] for item in targets if item["workflow_id"] not in platform.workflows]
        if missing:
            print(f"工作流未加载：{missing}", file=sys.stderr)
            return 2
        for item in targets:
            ok, payload = run_one(platform, item, args.timeout, args.json)
            results.append(payload)
            if not ok and not args.all:
                break
    finally:
        platform.close()

    if args.json:
        print(json.dumps(results if args.all else results[0], ensure_ascii=False, indent=2))
    else:
        total = len(results)
        passed = sum(1 for r in results if r.get("ok"))
        print(f"\n汇总：{passed}/{total} 个场景跑通「首轮失败 → 自循环修复 → 最终通过」。")

    return 0 if results and all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
