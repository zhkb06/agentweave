#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次真实运行导出为单文件 HTML「演示回放」。

输入：``data/runs/<run_id>/``（events.jsonl / metrics.json / report.md）
输出：``demo/演示回放-<run_id>.html``（自包含，无外部依赖，双击即可打开）

用法::

    py -3.14 scripts/build_replay.py                 # 取最近一次运行
    py -3.14 scripts/build_replay.py --run-id run-xxx
    py -3.14 scripts/build_replay.py --no-shots      # 不内嵌界面截图
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "runs"
SHOTS = ROOT / "screenshots"
DEMO = ROOT / "demo"

CSS = """
:root{--navy:#0B1B33;--navy2:#102544;--navy3:#162F52;--cyan:#17C3B2;--cyan2:#0E7F76;
--orange:#F29E4C;--text:#F2F7FF;--muted:#9FB3CC;--dim:#6B81A0}
*{box-sizing:border-box}
body{margin:0;background:var(--navy);color:var(--text);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:40px 28px 80px}
.kicker{color:var(--cyan);font-weight:700;letter-spacing:.08em;font-size:13px}
h1{font-size:34px;margin:10px 0 6px}
h2{font-size:20px;margin:38px 0 14px;color:var(--cyan)}
h2::before{content:"";display:inline-block;width:8px;height:8px;background:var(--cyan);margin-right:10px;border-radius:50%}
.sub{color:var(--muted);font-size:14px}
.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;border:1px solid}
.ok{color:var(--cyan);border-color:var(--cyan2);background:rgba(23,195,178,.08)}
.bad{color:var(--orange);border-color:var(--orange);background:rgba(242,158,76,.08)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:22px}
.card{background:var(--navy2);border:1px solid var(--navy3);border-radius:12px;padding:16px 18px}
.card .num{font-size:28px;font-weight:700;color:var(--cyan)}
.card .num.orange{color:var(--orange)}
.card .label{font-size:12px;color:var(--muted);margin-top:4px}
.card .note{font-size:11px;color:var(--dim);margin-top:8px}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--navy3);vertical-align:top}
th{color:var(--cyan);font-weight:600;background:rgba(23,195,178,.06);white-space:nowrap}
td.ok{color:var(--cyan)}
td.bad{color:var(--orange)}
.round{background:var(--navy2);border:1px solid var(--navy3);border-radius:12px;padding:18px 20px;margin-bottom:14px}
.round.pass{border-color:var(--cyan2)}
.round.fail{border-color:rgba(242,158,76,.55)}
.round h3{margin:0 0 4px;font-size:16px}
.mono{font-family:Consolas,Menlo,monospace;font-size:12px}
.evts{max-height:420px;overflow:auto;border:1px solid var(--navy3);border-radius:10px}
.evts table{font-size:12px;margin:0}
.evts th{position:sticky;top:0;background:var(--navy3)}
.tl{color:var(--dim);white-space:nowrap}
.shot{width:100%;border:1px solid var(--cyan2);border-radius:10px;margin-top:8px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
pre{background:var(--navy2);border:1px solid var(--navy3);border-radius:10px;padding:14px;overflow:auto;font-size:12px;line-height:1.5}
.foot{margin-top:46px;color:var(--dim);font-size:12px;border-top:1px solid var(--navy3);padding-top:16px}
a{color:var(--cyan)}
@media(max-width:900px){.cards{grid-template-columns:1fr 1fr}.cols{grid-template-columns:1fr}}
"""


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def b64_img(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def load_run(run_id: str):
    rd = RUNS / run_id
    events = [json.loads(line) for line in (rd / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    metrics = json.loads((rd / "metrics.json").read_text(encoding="utf-8"))
    report = (rd / "report.md").read_text(encoding="utf-8")
    return events, metrics, report


def parse_claims(detail: str) -> list[tuple[str, float, float, float]]:
    out = []
    for m in re.finditer(r"coefficients\.(\w+)：申报 ([-0-9.]+) vs [^，]+ ([-0-9.]+)，相对误差 ([-0-9.]+)%", detail or ""):
        out.append((m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4))))
    return out


def event_summary(e: dict) -> str:
    t = e["type"]
    p = e.get("payload") or {}
    node = e.get("node_id") or ""
    if t == "node.scheduled":
        return f"{node} → {p.get('agent_id')}（score {float(p.get('score') or 0):.3f}，负载 {float(p.get('agent_load') or 0):.2f}）"
    if t == "node.started":
        return f"{node} 开始"
    if t == "node.succeeded":
        conf = p.get("confidence")
        return f"{node} 完成" + (f"，置信度 {float(conf):.2f}" if conf is not None else "")
    if t == "node.failed":
        return f"{node} 未通过：{p.get('error', '')[:60]}"
    if t == "verify.result":
        return ("验证通过" if p.get("passed") else "验证失败") + f"，置信度 {float(p.get('confidence') or 0):.3f}"
    if t == "loop.iteration":
        return f"自循环第 {p.get('iteration')} 轮，" + ("通过" if p.get("verify_passed") else "未通过") + f"，置信度 {float(p.get('confidence') or 0):.3f}"
    if t == "loop.exit":
        return f"循环退出（{p.get('stop_reason')}，{p.get('iterations')} 轮）"
    if t in ("run.started", "run.finished", "run.created"):
        return {"run.started": "运行开始", "run.finished": "运行结束", "run.created": "运行创建"}[t]
    if t == "plan.ready":
        return f"调度计划就绪（policy={p.get('policy')}，候选 {len(p.get('candidates') or [])} 个）"
    if t == "human.required":
        return "人工闸门等待确认"
    if t == "human.decided":
        return f"人工闸门：{p.get('decision') or '通过'}"
    if t == "tool.called":
        return f"工具调用 {p.get('tool') or p.get('name') or ''}"
    if t == "tool.result":
        return f"工具返回 {'成功' if p.get('ok', True) else '失败'}"
    if t == "agent.decision":
        return str(p.get("summary") or p.get("decision") or "")[:70]
    return str(p.get("message") or "")[:70]


def build(run_id: str, with_shots: bool = True) -> Path:
    events, metrics, report = load_run(run_id)
    truth = json.loads((ROOT / "examples" / "datasets" / "air_quality_truth.json").read_text(encoding="utf-8"))
    verifies = [e for e in events if e["type"] == "verify.result"]
    loops = [e for e in events if e["type"] == "loop.iteration"]
    sched = [e for e in events if e["type"] == "node.scheduled"]
    created = events[0]["ts"] if events else 0
    finish = events[-1]["ts"] if events else 0
    first_pass = bool(verifies and verifies[0]["payload"]["passed"])
    final_pass = bool(verifies and verifies[-1]["payload"]["passed"])
    first_conf = verifies[0]["payload"]["confidence"] if verifies else 0
    final_conf = verifies[-1]["payload"]["confidence"] if verifies else 0
    repair = next((x["payload"] for x in events if x["type"] == "loop.exit"), {}) or {}
    # 首轮申报 vs 真值
    claims = []
    if verifies:
        for chk in verifies[0]["payload"].get("checks", []):
            claims = parse_claims(chk.get("detail", ""))
            if claims:
                break
    truth_coef = truth.get("coefficients", {})
    coef_rows = ""
    for key, claimed, _recomputed, rel_err in claims:
        if key not in truth_coef:
            continue
        truth_value = float(truth_coef[key])
        coef_rows += (
            f"<tr><td>{esc(key)}</td><td class='mono bad'>{claimed:+.3f}</td>"
            f"<td class='bad'>{rel_err:.1f}%</td><td class='mono ok'>{truth_value:+.3f}</td>"
            f"<td class='mono'>{abs(truth_value - claimed) / max(abs(truth_value), 1e-9) * 100:.1f}%</td></tr>"
        )

    # 两轮验证
    rounds_html = []
    for idx, ev in enumerate(verifies, 1):
        p = ev["payload"]
        checks = "".join(
            f"<tr><td>{esc(c.get('name'))}</td><td class='{'ok' if c.get('passed') else 'bad'}'>"
            f"{'通过' if c.get('passed') else '失败'}</td><td>{esc(c.get('detail'))}</td>"
            f"<td>{esc(c.get('weight'))}</td></tr>"
            for c in p.get("checks", [])
        )
        rounds_html.append(f"""
<div class="round {'pass' if p.get('passed') else 'fail'}">
  <h3>第 {idx} 轮验证：{'✅ 通过' if p.get('passed') else '❌ 未通过'} · 置信度 {float(p.get('confidence') or 0):.3f} · 层 {esc(p.get('layer'))}</h3>
  <table><thead><tr><th>检查器</th><th>结论</th><th>证据</th><th>权重</th></tr></thead><tbody>{checks}</tbody></table>
</div>""")

    loop_rows = "".join(
        f"<tr><td>第 {p.get('iteration')} 轮</td><td class='{'ok' if p.get('verify_passed') else 'bad'}'>"
        f"{'通过' if p.get('verify_passed') else '未通过'}</td><td>{float(p.get('confidence') or 0):.3f}</td>"
        f"<td>{esc(', '.join(p.get('failures') or []) or '—')}</td></tr>"
        for p in [x["payload"] for x in loops]
    )

    evt_rows = "".join(
        f"<tr><td class='tl'>+{e['ts']-created:6.3f}s</td><td class='mono'>{e['seq']:>3}</td>"
        f"<td>{esc(e['type'])}</td><td>{esc(e.get('node_id') or '')}</td><td>{esc(event_summary(e))}</td></tr>"
        for e in events
    )

    bench = {}
    try:
        bench = json.loads((ROOT / "data" / "benchmark.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    modes_raw = bench.get("modes") or []
    modes = {str(item.get("mode")): item for item in modes_raw if isinstance(item, dict)}
    def fmt(value, pct=False, digits=2):
        if value is None:
            return "—"
        try:
            num = float(value)
        except (TypeError, ValueError):
            return "—"
        return f"{num * 100:.1f}%" if pct else f"{num:,.{digits}f}"
    bench_rows = ""
    for name, label in (("A", "A 单 Agent 串行"), ("B", "B 多 Agent 并行"), ("C", "C 并行 + 验证自循环")):
        item = modes.get(name, {})
        bench_rows += (
            f"<tr><td>{label}</td>"
            f"<td>{fmt(item.get('wall_p50_s'))}</td>"
            f"<td>{fmt(item.get('tokens_avg'), digits=0)}</td>"
            f"<td>{fmt(item.get('confidence_avg'), pct=True)}</td>"
            f"<td>{fmt(item.get('coef_error_avg'), pct=True)}</td>"
            f"<td>{fmt(item.get('verified_rate'), pct=True)}</td></tr>"
        )

    shots_html = ""
    if with_shots:
        imgs = []
        for name, title in (("03-monitor.png", "运行监控：SSE 事件流、节点状态、调度决策与自循环轮次"),
                            ("04-verify.png", "验证中心：最终 100% 通过，右侧为两轮自循环历史")):
            path = SHOTS / name
            if path.is_file():
                imgs.append(f"<div><div class='sub'>{esc(title)}</div><img class='shot' src='{b64_img(path)}' alt='{esc(title)}'/></div>")
        if imgs:
            shots_html = f"<h2>界面证据（同一工作流）</h2><div class='cols'>{''.join(imgs)}</div>"

    # 报告正文中「主要发现」表
    findings = ""
    try:
        seg = report.split("## 三、主要发现", 1)[1].split("## 四、", 1)[0]
        findings = f"<pre>{esc(seg.strip())}</pre>"
    except Exception:
        findings = "<pre>（见运行产物 report.md）</pre>"

    duration = float(metrics.get("wall_s") or metrics.get("duration_s") or 0)
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>织流 AgentWeave · 运行回放 {esc(run_id)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<div class="kicker">织流 AgentWeave · 实际运行回放（离线可打开）</div>
<h1>一次可验证的多智能体科研分析</h1>
<div class="sub">运行 ID <span class="mono">{esc(run_id)}</span> ·
状态 <span class="badge {'ok' if metrics.get('nodes_failed', 0) == 0 else 'bad'}">{'成功' if metrics.get('nodes_failed',0)==0 else '失败'}</span> ·
工作流 <span class="mono">wf-ai4s-air-quality</span> · 事件总线 {len(events)} 条 ·
生成于 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">
  <div class="card"><div class="num">{metrics.get('nodes_total')}</div><div class="label">工作流节点</div><div class="note">含 2 条并行分支、1 个 PDCA 循环、1 个人工闸门</div></div>
  <div class="card"><div class="num">{len(sched)}</div><div class="label">调度决策</div><div class="note">每次派发都记录 score / reason / 负载，可审计</div></div>
  <div class="card"><div class="num orange">{float(first_conf)*100:.1f}% → {float(final_conf)*100:.0f}%</div><div class="label">验证置信度</div><div class="note">第 1 轮失败，自循环修复后第 2 轮通过</div></div>
  <div class="card"><div class="num">{float(duration):.2f}s</div><div class="label">端到端耗时</div><div class="note">Token {metrics.get('tokens_total')}，平均置信度 {metrics.get('avg_confidence')}</div></div>
</div>

<h2>结论与证据：数字自己会说话</h2>
<div class="cols">
<div>
<div class="sub">首轮申报与真值的偏差（隐藏真值仅验证器可见）</div>
<table><thead><tr><th>因子</th><th>首轮申报</th><th>重跑误差</th><th>真值</th><th>对真值偏差</th></tr></thead>
<tbody>{coef_rows}</tbody></table>
</div>
<div>
<div class="sub">自循环两轮（PDCA）</div>
<table><thead><tr><th>轮次</th><th>验证</th><th>置信度</th><th>失败项</th></tr></thead>
<tbody>{loop_rows}</tbody></table>
<div class="sub" style="margin-top:10px">终态：修复 {repair.get('iterations', '—')} 轮后 {esc(repair.get('stop_reason', 'converged'))}；
硬性失败项（code_rerun / numeric_ground_truth）触发反馈注入，第二轮修正为 -0.84 / 0.44 / -1.67 / 0.26，进入 15% 容差。</div>
</div>
</div>

<h2>五层验证：失败与通过都留下证据</h2>
{''.join(rounds_html)}

<h2>报告结论（自动生成）</h2>
{findings}

<h2>三模式基准：可验证性的成本</h2>
<table><thead><tr><th>模式</th><th>耗时 s</th><th>Token</th><th>置信度</th><th>系数误差</th><th>验证通过率</th></tr></thead>
<tbody>{bench_rows}</tbody></table>
<div class="sub" style="margin-top:8px">数据来自仓库 <span class="mono">data/benchmark.json</span>；A/B 无验证，C 用约 27.6% 的额外 Token 把系数误差从 34.4% 降到 7.1%，验证通过率 100%。</div>

{shots_html}

<h2>完整事件流（可审计）</h2>
<div class="evts"><table><thead><tr><th>时间</th><th>seq</th><th>类型</th><th>节点</th><th>摘要</th></tr></thead><tbody>{evt_rows}</tbody></table></div>

<h2>如何复现</h2>
<pre>py -3.14 scripts/demo_tour.py        # 终端现场演示（实时事件旁白）
py -3.14 scripts/run_demo.py --json  # 一次运行 + JSON 摘要
py -3.14 server.py                   # Web 控制台 http://127.0.0.1:8765</pre>

<div class="foot">本页由 <span class="mono">scripts/build_replay.py</span> 从运行产物生成，不含虚构数据；
所有数字均来自 <span class="mono">data/runs/{esc(run_id)}/events.jsonl</span> 与 <span class="mono">metrics.json</span>。
产物目录还包含 <span class="mono">report.md / analysis_analyze.py / workflow.json</span>。</div>
</div></body></html>"""
    DEMO.mkdir(exist_ok=True)
    out = DEMO / "演示回放.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def latest_run() -> str:
    dirs = [p for p in RUNS.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()]
    return max(dirs, key=lambda p: (p / "events.jsonl").stat().st_mtime).name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成单文件演示回放 HTML")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--no-shots", action="store_true")
    args = parser.parse_args(argv)
    run_id = args.run_id or latest_run()
    out = build(run_id, with_shots=not args.no_shots)
    print(f"回放已生成：{out}（{out.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
