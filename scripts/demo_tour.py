#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""织流 AgentWeave · 终端现场演示（把一次运行讲给评委听）。

与 ``run_demo.py`` 的区别
-------------------------
``run_demo.py`` 是「跑完给结果」；本脚本是「边跑边讲」：
它订阅事件总线，把规划、调度、并行、验证失败、反馈修复、通过、报告
实时翻译成人类可读的演示旁白，适合现场投屏或录屏。

用法::

    py -3.14 scripts/demo_tour.py            # 完整演示（默认）
    py -3.14 scripts/demo_tour.py --fast     # 精简：不打印节点级流水
    py -3.14 scripts/demo_tour.py --no-color # 关掉 ANSI 颜色（重定向到文件时）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.types import RunRequest  # noqa: E402
from agentweave.demo import scenario as demo  # noqa: E402
from agentweave.runtime.platform import create_platform, load_platform_config  # noqa: E402

TERMINAL = {"succeeded", "failed", "cancelled"}

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "blue": "\033[34m", "mag": "\033[35m",
}


class Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled:
            return text
        return "".join(C.get(s, "") for s in styles) + text + C["reset"]


def line(p: Painter, act: str, text: str, color: str = "cyan") -> None:
    print(f"{p(act, 'bold', color)} {text}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentWeave 终端现场演示")
    parser.add_argument("--fast", action="store_true", help="精简节点级流水")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    use_color = (not args.no_color) and (os.environ.get("NO_COLOR") is None) and sys.stdout.isatty()
    p = Painter(use_color)
    scenario = demo.summary()
    truth = scenario.get("ground_truth") or {}

    print()
    print(p("  织流 AgentWeave · 多智能体编排、调度与可信验证平台", "bold", "cyan"))
    print(p(f"  演示场景：{scenario.get('title')}", "dim"))
    print(p(f"  任务：{scenario.get('task')}", "dim"))
    print()
    line(p, "目标", "证明三件事：流程可编排、结果可验证、失败能自愈。", "bold")

    config = load_platform_config()
    platform = create_platform(config)
    started = time.time()
    sub = None
    try:
        platform.start()
        request = RunRequest(
            workflow_id=demo.WORKFLOW_ID,
            inputs={"task": demo.TASK_PROMPT, "dataset": demo.DATASET_REL},
            policy="adaptive",
            label="现场演示",
            scenario=demo.SCENARIO_ID,
            auto_approve=True,
        )
        run_id = platform.start_run(request)
        sub = platform.subscribe(run_id)
        line(p, "① 提交", f"运行 {run_id} · 工作流 {request.workflow_id} · 策略 adaptive", "green")
        print(p("  （以下为事件总线实时输出，与 Web 页面 SSE 完全同源）", "dim"))
        print()

        verify_round = 0
        loop_round = 0
        last_verify = None
        while time.time() - started < args.timeout:
            event = sub.get(timeout=0.3)
            if event is not None:
                kind = getattr(event, "type", "")
                payload = getattr(event, "payload", {}) or {}
                node = getattr(event, "node_id", "") or ""
                if kind == "run.started":
                    line(p, "② 启动", "引擎开始按 DAG 拓扑推进（12 节点，含 2 条并行分支）", "green")
                elif kind == "plan.ready" and not args.fast:
                    cands = payload.get("candidates") or []
                    line(p, "   调度", f"候选 {len(cands)} 个，按 score 排序；policy={payload.get('policy')}", "blue")
                elif kind == "node.scheduled" and not args.fast:
                    print(p(f"   → 派发 {node} 给 {payload.get('agent_id')}"
                            f"（score {float(payload.get('score') or 0):.3f}，负载 {float(payload.get('agent_load') or 0):.2f}）", "dim"))
                elif kind == "node.started" and not args.fast:
                    if node in ("scan", "research"):
                        print(p(f"   ▶ {node} 开始（并行分支，互不阻塞）", "dim"))
                elif kind == "node.succeeded" and not args.fast:
                    if payload.get("confidence") is not None:
                        print(p(f"   ✓ {node} 完成，置信度 {float(payload['confidence']):.2f}", "dim"))
                elif kind == "node.failed":
                    print(p(f"   ✗ {node} 验证未通过（这是演示的设计点）", "yellow"))
                elif kind == "verify.result":
                    verify_round += 1
                    last_verify = payload
                    checks = payload.get("checks") or []
                    failed = [c for c in checks if not c.get("passed")]
                    color = "green" if payload.get("passed") else "yellow"
                    line(p, f"③ 验证-{verify_round}",
                         f"{'通过' if payload.get('passed') else '失败'} · 置信度 {float(payload.get('confidence') or 0):.3f}"
                         f" · 检查 {len(checks)} 项（失败 {len(failed)}）", color)
                    for c in failed:
                        print(p(f"   - {c.get('name')}: {c.get('detail')}", "yellow"))
                elif kind == "loop.iteration":
                    loop_round += 1
                    line(p, f"④ 自循环-{loop_round}",
                         f"第 {payload.get('iteration')}/{payload.get('max_iterations')} 轮 · "
                         f"验证{'通过' if payload.get('verify_passed') else '未通过'} · "
                         f"置信度 {float(payload.get('confidence') or 0):.3f}", "mag")
                    fb = (payload.get("feedback_preview") or "").strip()
                    if fb and not payload.get("verify_passed"):
                        print(p(f"   反馈注入修复节点：{fb[:120].replace(chr(10), ' ')}...", "dim"))
                elif kind == "loop.exit":
                    line(p, "   收敛", f"停止原因 {payload.get('stop_reason')} · 共 {payload.get('iterations')} 轮", "mag")
                elif kind == "human.required":
                    print(p(f"   人工闸门：{payload.get('prompt') or ''}（演示模式自动放行）", "dim"))
                elif kind == "run.finished":
                    pass
            run = platform.get_run(run_id) or {}
            if str(run.get("status")) in TERMINAL:
                # 把队列里剩余事件读完
                while True:
                    ev = sub.get(timeout=0.05)
                    if ev is None:
                        break
                break
        else:
            print(p("演示超时", "red"))
            return 1

        run = platform.get_run(run_id) or {}
        metrics = run.get("metrics") or {}
        verification = platform.get_verification(run_id)
        loop = platform.get_loop_status(run_id) or {}
        repair = loop.get("repair") or {}
        report = platform.get_report(run_id) or {}

        print()
        print(p("  ────────────────── 演示结果 ──────────────────", "bold", "cyan"))
        status = str(run.get("status"))
        color = "green" if status == "succeeded" else "red"
        print(p(f"  状态            {status}", "bold", color))
        print(f"  耗时            {metrics.get('wall_s', metrics.get('duration_s'))} s")
        print(f"  节点            {metrics.get('nodes_total')} 个，成功 {metrics.get('nodes_succeeded')} 个")
        print(f"  Token           {metrics.get('tokens_total')}（入 {metrics.get('tokens_in')} / 出 {metrics.get('tokens_out')}）")
        print(f"  平均置信度      {metrics.get('avg_confidence')}")
        print(f"  验证轮次        {len(verification)}（最终：{'通过' if verification and verification[-1].get('passed') else '未通过'}）")
        print(f"  自循环          {repair.get('iterations')} 轮（converged={repair.get('converged')}）")

        # 第一轮失败 vs 第二轮修复的系数（从验证证据里取，与真值对比）
        if verification:
            print()
            print(p("  系数对比（第一轮申报 → 第二轮申报 → 真值）", "bold"))
            print(f"  {'因子':<8}{'第一轮':>10}{'第二轮':>10}{'真值':>10}")
            first, last = verification[0], verification[-1]
            claimed_first = {}
            for chk in first.get("checks") or []:
                for item in (chk.get("evidence") or {}).get("failures") or []:
                    path = str(item.get("path") or "")
                    if path.startswith("coefficients."):
                        claimed_first[path.split(".", 1)[1]] = item.get("actual")
            second = {}
            for node_id, state in (platform.get_run(run_id) or {}).get("nodes", {}).items():
                out = (state or {}).get("output") or {}
                m = out.get("metrics") if isinstance(out, dict) else None
                if isinstance(m, dict) and isinstance(m.get("coefficients"), dict):
                    second = m["coefficients"]
            for key, tv in (truth or {}).items():
                f = claimed_first.get(key)
                s = second.get(key)
                print(f"  {key:<8}{(f'{f:+.3f}' if isinstance(f, (int, float)) else '   -'):>10}"
                      f"{(f'{s:+.3f}' if isinstance(s, (int, float)) else '   -'):>10}"
                      f"{(f'{tv:+.3f}' if isinstance(tv, (int, float)) else '   -'):>10}")

        report_path = report.get("path") or ""
        print()
        print(p(f"  报告            {report_path or '（未生成）'}", "dim"))
        artifacts_dir = Path(report.get("path") or "").parent if report.get("path") else Path("data/runs") / run_id
        print(p(f"  产物目录        {artifacts_dir}", "dim"))
        print()
        print(p("  一句话总结：流程是数据（可改）、调度有理由（可查）、结果是证据（可验）、失败会自愈（可控）。",
                 "bold", "cyan"))
        print()
        return 0 if str(run.get("status")) == "succeeded" and verification and verification[-1].get("passed") else 1
    finally:
        if sub is not None:
            try:
                sub.close()
            except Exception:
                pass
        platform.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
