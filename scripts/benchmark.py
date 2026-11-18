"""性能基准：对同一演示工作流重复运行，度量端到端时延、事件吞吐与调度开销。

用途
----
1. 方案书「测试与性能评估」章节需要**可复现的实验数据**；
2. 决赛压测对比基线（Mock 离线模式 vs 真实 LLM 模式）可直接复用本脚本；
3. 回归：改动调度器/引擎后对比 ``p95`` 与 ``success_rate`` 是否退化。

用法::

    py -3.14 scripts/benchmark.py                      # 默认 3 次，adaptive 策略
    py -3.14 scripts/benchmark.py --runs 5 --policy capability
    py -3.14 scripts/benchmark.py --json               # 只输出 JSON
    py -3.14 scripts/benchmark.py --concurrency 2      # 同时提交 2 个运行

输出：``data/benchmark.json``（完整数据）+ 终端 Markdown 表格。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import RunRequest  # noqa: E402
from agentweave.demo import scenario as demo  # noqa: E402
from agentweave.runtime.platform import create_platform, load_platform_config  # noqa: E402


def make_request(policy: str, index: int, label: str) -> RunRequest:
    payload = demo.run_request_payload(f"{label} #{index}")
    return RunRequest(
        workflow_id=payload["workflow_id"],
        inputs=payload["inputs"],
        policy=policy,
        label=payload["label"],
        scenario=payload["scenario"],
        auto_approve=payload["auto_approve"],
    )


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


# ---------------------------------------------------------------------------
# 三模式对照（A 单 Agent / B 多 Agent 并行 / C 多 Agent + 验证自循环）
# ---------------------------------------------------------------------------
def build_mode_a() -> dict:
    """模式 A：单 Agent 一次完成全部分析，无协作、无验证。"""
    return {
        "id": "wf-bench-single-agent",
        "name": "基准A：单 Agent 串行",
        "version": "1.0.0",
        "description": "一个统计 Agent 顺序完成取数、清洗、建模与结论，无验证。",
        "owner": "benchmark",
        "variables": {
            "input": {
                "task": demo.TASK_PROMPT,
                "dataset": "examples/datasets/air_quality.csv",
            },
            "dataset_path": "examples/datasets/air_quality.csv",
        },
        "nodes": [
            {"id": "start", "type": "start", "label": "开始"},
            {
                "id": "analyze",
                "type": "agent",
                "label": "单 Agent 一次分析",
                "agent": "statistician",
                "prompt": (
                    "一次性完成：读取 examples/datasets/air_quality.csv，"
                    "估计 PM2.5 对 temp/humidity/wind/so2 的多元线性回归系数，"
                    "给出 r2 与 n，输出 JSON。不要等待任何验证。"
                ),
            },
            {"id": "end", "type": "end", "label": "结束"},
        ],
        "edges": [
            {"source": "start", "target": "analyze"},
            {"source": "analyze", "target": "end"},
        ],
    }


def build_mode_b(demo_dict: dict) -> dict:
    """模式 B：多 Agent 并行协作，但去掉验证与自循环（只跑一遍）。"""
    variant = json.loads(json.dumps(demo_dict, ensure_ascii=False))
    variant["id"] = "wf-bench-multi-agent"
    variant["name"] = "基准B：多 Agent 并行（无自循环）"
    variant["description"] = "7 角色分工与并行分支，但无验证节点、无 PDCA 修复。"
    removed = {"repair", "verify"}
    variant["nodes"] = [n for n in variant["nodes"] if n.get("id") not in removed]
    edges = [
        e
        for e in variant["edges"]
        if e.get("source") not in removed and e.get("target") not in removed
    ]
    edges.extend(
        [
            {"source": "clean", "target": "analyze", "label": "直接建模"},
            {"source": "analyze", "target": "critique", "label": "同行评审"},
        ]
    )
    variant["edges"] = edges
    return variant


def register_variant(platform, payload: dict) -> str:
    from agentweave.core.types import Workflow

    wf = Workflow.from_dict(payload)
    errors = wf.validate()
    if errors:
        raise ValueError(f"基准工作流校验失败：{errors}")
    platform.workflows[wf.id] = wf
    return wf.id


def coefficient_error(run: dict, truth: dict) -> float:
    node = (run.get("nodes") or {}).get("analyze") or {}
    output = node.get("output") or {}
    metrics = output.get("metrics") if isinstance(output, dict) else None
    if not isinstance(metrics, dict):
        metrics = output if isinstance(output, dict) else {}
    claimed = metrics.get("coefficients") if isinstance(metrics, dict) else None
    if not isinstance(claimed, dict):
        return 999.0
    truth_coef = (truth or {}).get("coefficients") or {}
    worst = 0.0
    for key, expected in truth_coef.items():
        try:
            actual = float(claimed.get(key))
            expected = float(expected)
        except (TypeError, ValueError):
            return 999.0
        scale = abs(expected) if abs(expected) > 1e-9 else 1.0
        worst = max(worst, abs(actual - expected) / scale)
    return round(worst, 4)


def run_mode(platform, mode: str, workflow_id: str, runs: int, timeout: float) -> dict:
    rows: list[dict] = []
    truth = demo.load_truth()
    for index in range(1, runs + 1):
        request = make_request("adaptive", index, f"bench-{mode}")
        request.workflow_id = workflow_id
        started = time.time()
        run_id = platform.start_run(request)
        platform.wait_run(run_id, timeout=timeout)
        wall = time.time() - started
        run = platform.get_run(run_id) or {}
        verification = platform.get_verification(run_id)
        metrics = run.get("metrics") or {}
        final = verification[-1] if verification else None
        rows.append(
            {
                "mode": mode,
                "run_id": run_id,
                "status": run.get("status"),
                "wall_s": round(wall, 3),
                "tokens": metrics.get("tokens_total", 0),
                "confidence": round(float(metrics.get("avg_confidence") or 0.0), 4),
                "coef_error": coefficient_error(run, truth),
                "verified": bool(final and final.get("passed")),
                "verifications": len(verification),
            }
        )
    walls = [row["wall_s"] for row in rows]
    return {
        "mode": mode,
        "runs": runs,
        "wall_median_s": round(statistics.median(walls), 3) if walls else 0.0,
        "wall_p50_s": round(percentile(walls, 0.50), 3),
        "wall_p95_s": round(percentile(walls, 0.95), 3),
        "tokens_avg": round(statistics.mean(r["tokens"] for r in rows), 1) if rows else 0,
        "confidence_avg": round(statistics.mean(r["confidence"] for r in rows), 4) if rows else 0.0,
        "coef_error_avg": round(statistics.mean(r["coef_error"] for r in rows), 4) if rows else 1.0,
        "verified_rate": round(sum(1 for r in rows if r["verified"]) / len(rows), 3) if rows else 0.0,
        "detail": rows,
    }


def run_once(platform, policy: str, index: int, label: str, timeout: float) -> dict:
    started = time.time()
    run_id = platform.start_run(make_request(policy, index, label))
    summary = platform.wait_run(run_id, timeout=timeout)
    wall = time.time() - started
    run = platform.get_run(run_id) or {}
    events = platform.get_events(run_id, 0, 20000)
    verification = platform.get_verification(run_id)
    loop = platform.get_loop_status(run_id)
    loop_nodes = loop.get("self_loop") if isinstance(loop.get("self_loop"), dict) else loop
    loop_main = loop_nodes.get("repair") or {}
    if not loop_main:
        for value in loop_nodes.values():
            if int(value.get("iterations") or 0) > 0:
                loop_main = value
                break
    metrics = run.get("metrics") or {}
    node_count = len(run.get("nodes") or {})
    return {
        "run_id": run_id,
        "ok": str(run.get("status")) == "succeeded" and bool(summary),
        "status": run.get("status"),
        "wall_s": round(wall, 3),
        "engine_s": metrics.get("duration_s", 0),
        "nodes": node_count,
        "events": len(events),
        "events_per_s": round(len(events) / wall, 2) if wall > 0 else 0.0,
        "tokens": metrics.get("tokens_total", 0),
        "self_loop_iterations": int(loop_main.get("iterations") or 0),
        "verifications": len(verification),
        "avg_confidence": metrics.get("avg_confidence", 0),
        "success_rate": metrics.get("success_rate", 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="织流 AgentWeave 性能基准")
    parser.add_argument("--runs", type=int, default=3, help="顺序运行次数（默认 3）")
    parser.add_argument("--policy", default="adaptive", help="调度策略：adaptive/capability/priority/round_robin/memory")
    parser.add_argument("--timeout", type=float, default=240.0, help="单次运行超时秒数")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--out", default="data/benchmark.json", help="结果输出路径")
    args = parser.parse_args(argv)

    if args.runs < 1:
        print("--runs 必须 ≥ 1", file=sys.stderr)
        return 2

    platform = create_platform(load_platform_config())
    rows: list[dict] = []
    modes: list[dict] = []
    try:
        platform.start()
        warmup = run_once(platform, args.policy, 0, "warmup", args.timeout)
        if not args.json:
            print(f"[warmup] status={warmup['status']} wall={warmup['wall_s']}s events={warmup['events']}")
        for index in range(1, args.runs + 1):
            row = run_once(platform, args.policy, index, "benchmark", args.timeout)
            rows.append(row)
            if not args.json:
                print(
                    f"[run {index}/{args.runs}] status={row['status']} wall={row['wall_s']}s "
                    f"events={row['events']} loop={row['self_loop_iterations']}"
                )
        # 三模式对照：A 单 Agent / B 多 Agent 并行 / C 多 Agent + 验证自循环
        demo_wf = platform.workflows.get(demo.WORKFLOW_ID)
        if demo_wf is not None:
            register_variant(platform, build_mode_a())
            register_variant(platform, build_mode_b(demo_wf.to_dict()))
            for mode, workflow_id in (
                ("A", "wf-bench-single-agent"),
                ("B", "wf-bench-multi-agent"),
                ("C", demo.WORKFLOW_ID),
            ):
                result = run_mode(platform, mode, workflow_id, args.runs, args.timeout)
                modes.append(result)
                if not args.json:
                    print(
                        f"[模式 {mode}] wall={result['wall_median_s']}s tokens={result['tokens_avg']} "
                        f"conf={result['confidence_avg']} coef_err={result['coef_error_avg']} "
                        f"verified={result['verified_rate']}"
                    )
    finally:
        platform.close()

    walls = [row["wall_s"] for row in rows]
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "policy": args.policy,
        "runs": args.runs,
        "llm": "mock-deterministic (offline)",
        "node_count": rows[0]["nodes"] if rows else 0,
        "summary": {
            "success_rate": round(sum(1 for row in rows if row["ok"]) / len(rows), 3) if rows else 0,
            "wall_avg_s": round(statistics.mean(walls), 3) if walls else 0,
            "wall_p50_s": round(percentile(walls, 0.50), 3),
            "wall_p95_s": round(percentile(walls, 0.95), 3),
            "wall_min_s": round(min(walls), 3) if walls else 0,
            "wall_max_s": round(max(walls), 3) if walls else 0,
            "events_avg": round(statistics.mean([row["events"] for row in rows]), 1) if rows else 0,
            "events_per_s_avg": round(
                statistics.mean([row["events_per_s"] for row in rows]), 2
            ) if rows else 0,
            "tokens_avg": round(statistics.mean([row["tokens"] for row in rows]), 1) if rows else 0,
            "self_loop_avg": round(
                statistics.mean([row["self_loop_iterations"] for row in rows]), 2
            ) if rows else 0,
        },
        "runs_detail": rows,
        "modes": modes,
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print("\n| 指标 | 数值 |\n| --- | --- |")
        print(f"| 运行次数 | {args.runs} |")
        print(f"| 成功率 | {s['success_rate'] * 100:.0f}% |")
        print(f"| 平均端到端时延 | {s['wall_avg_s']}s |")
        print(f"| P50 / P95 时延 | {s['wall_p50_s']}s / {s['wall_p95_s']}s |")
        print(f"| 事件吞吐 | {s['events_per_s_avg']}/s（平均 {s['events_avg']} 条） |")
        print(f"| 平均 Token | {s['tokens_avg']} |")
        print(f"| 平均自循环 | {s['self_loop_avg']} 轮 |")
        if modes:
            print("\n| 模式 | 端到端时延(中位) | 成功率(验证通过) | 平均置信度 | 系数相对误差 | Token |")
            print("|---|---|---|---|---|---|")
            for mode in modes:
                print(
                    f"| {mode['mode']} | {mode['wall_median_s']}s | "
                    f"{mode['verified_rate'] * 100:.0f}% | {mode['confidence_avg'] * 100:.1f}% | "
                    f"{mode['coef_error_avg'] * 100:.2f}% | {mode['tokens_avg']} |"
                )
        print(f"\n结果已写入：{out}")
    return 0 if report["summary"]["success_rate"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
