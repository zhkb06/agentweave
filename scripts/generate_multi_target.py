#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「多目标联合分析」场景包（数据 + 真值 + 场景描述 + 工作流）。

与单目标场景的区别：
- 一条样本同时观测多个目标列（目标数量不限）；
- 每个目标各自做多元回归，指标以 ``<目标>.<因子>`` 扁平键汇总；
- 工作流在 15 节点基础上再增加一个「联合对比」节点：比较各目标的共同驱动与差异因子。

用法::

    py -3.14 scripts/generate_multi_target.py
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
BASE_WORKFLOW = ROOT / "workflows" / "ai4s_air_quality.json"

# feature: (列名, 均值, 标准差, 下限, 上限)
# targets: name / intercept / noise_sd / coefficients(因子->系数)
MULTI_SCENARIOS: list[dict] = [
    {
        "id": "manufacturing-quality-ops",
        "short": "mfg-ops",
        "workflow_id": "wf-manufacturing-quality-ops",
        "name": "制造质量-能耗联合分析",
        "title": "产线良品率与单位能耗的多目标联合归因",
        "description": "同一批产线样本同时观测良品率（yield_pct）与单位能耗（energy_kwh），"
                       "联合估计工艺参数对两个目标的影响，识别「提质量」与「降能耗」的共同抓手与冲突项。",
        "dataset": "scenarios/manufacturing-quality-ops/data.csv",
        "seed": 20261111,
        "n": 3000,
        "features": [
            ("temp", 22.0, 3.0, 15.0, 30.0),
            ("pressure", 3.0, 0.4, 1.8, 4.2),
            ("line_speed", 12.0, 2.0, 6.0, 18.0),
            ("humidity", 45.0, 8.0, 20.0, 70.0),
            ("output", 100.0, 15.0, 40.0, 160.0),
        ],
        "targets": [
            {
                "name": "yield_pct",
                "intercept": 98.0,
                "noise_sd": 1.8,
                "coefficients": {"temp": -0.45, "pressure": 2.20, "line_speed": -1.10,
                                 "humidity": -0.10, "output": 0.02},
            },
            {
                "name": "energy_kwh",
                "intercept": 60.0,
                "noise_sd": 10.0,
                "coefficients": {"temp": 1.60, "pressure": 3.50, "line_speed": 2.20,
                                 "humidity": 0.40, "output": 0.90},
            },
        ],
        "first_bias": {"temp": 0.300, "pressure": 0.320, "line_speed": 0.290,
                       "humidity": 0.330, "output": 0.310},
        "second_bias": {"temp": 0.030, "pressure": 0.038, "line_speed": 0.029,
                        "humidity": 0.040, "output": 0.032},
        "task": "对产线样本同时分析良品率（yield_pct）与单位能耗（energy_kwh）两个目标，"
                "分别估计温度（temp）、压力（pressure）、线速（line_speed）、湿度（humidity）、产量（output）"
                "对两个目标的影响，并做联合对比：哪些因子是共同抓手，哪些存在目标冲突。",
        "story": "工艺部门要同时守住良品率与能耗指标，但直觉上「提压力、降线速」对质量有利，却可能推高能耗。"
                 "需要联合分析来识别真正的双赢参数与冲突参数。",
        "domain_checks": ["核对两个目标的量纲与采集口径", "检查工艺参数共线性", "区分短期调参与长期漂移"],
    },
    {
        "id": "marketing-growth",
        "short": "mkt-growth",
        "workflow_id": "wf-marketing-growth",
        "name": "营销增长三目标联合分析",
        "title": "用户流失、生命周期价值与获客成本的多目标归因",
        "description": "同一批用户样本同时观测流失率（churn_pct）、生命周期价值（ltv）与获客成本（cac），"
                       "联合估计满意度、投诉、在网时长、价格指数、广告投入对三个目标的影响，支持预算联合优化。",
        "dataset": "scenarios/marketing-growth/data.csv",
        "seed": 20261112,
        "n": 3000,
        "features": [
            ("satisfaction", 70.0, 12.0, 20.0, 100.0),
            ("complaints", 30.0, 15.0, 0.0, 90.0),
            ("tenure", 24.0, 10.0, 1.0, 60.0),
            ("price_index", 100.0, 10.0, 70.0, 140.0),
            ("ad_spend", 25.0, 6.0, 5.0, 50.0),
        ],
        "targets": [
            {
                "name": "churn_pct",
                "intercept": 34.0,
                "noise_sd": 2.2,
                "coefficients": {"satisfaction": -0.22, "complaints": 0.07, "tenure": -0.14,
                                 "price_index": 0.10, "ad_spend": 0.02},
            },
            {
                "name": "ltv",
                "intercept": 120.0,
                "noise_sd": 8.0,
                "coefficients": {"satisfaction": 0.80, "complaints": -0.30, "tenure": 2.20,
                                 "price_index": -0.10, "ad_spend": 0.15},
            },
            {
                "name": "cac",
                "intercept": 50.0,
                "noise_sd": 5.0,
                "coefficients": {"satisfaction": -0.40, "complaints": 0.05, "tenure": -0.50,
                                 "price_index": 0.20, "ad_spend": 0.90},
            },
        ],
        "first_bias": {"satisfaction": 0.310, "complaints": 0.290, "tenure": 0.330,
                       "price_index": 0.300, "ad_spend": 0.320},
        "second_bias": {"satisfaction": 0.034, "complaints": 0.027, "tenure": 0.040,
                        "price_index": 0.030, "ad_spend": 0.037},
        "task": "对用户样本同时分析流失率（churn_pct）、生命周期价值（ltv）、获客成本（cac）三个目标，"
                "分别估计满意度（satisfaction）、投诉量（complaints）、在网时长（tenure）、价格指数（price_index）、"
                "广告投入（ad_spend）的影响，并做联合对比：哪些投入同时改善多个目标，哪些存在此消彼长。",
        "story": "增长团队只有一笔预算：降价、投广告、补客服还是做满意度项目？三个目标（留存、LTV、CAC）"
                 "往往互相牵制，需要联合分析而不是逐个拍脑袋。",
        "domain_checks": ["确认三个目标的统计口径与时间窗", "检查广告投入的内生性", "区分新老用户结构"],
    },
]


# ---------------------------------------------------------------------------
# 数值工具
# ---------------------------------------------------------------------------
def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(matrix)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("奇异矩阵")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor == 0:
                continue
            for j in range(col, n + 1):
                a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def ols(rows: list[list[float]], target: list[float]) -> tuple[list[float], float]:
    k = len(rows[0])
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for row, y in zip(rows, target):
        for i in range(k):
            xty[i] += row[i] * y
            for j in range(k):
                xtx[i][j] += row[i] * row[j]
    beta = solve(xtx, xty)
    pred = [sum(b * x for b, x in zip(beta, row)) for row in rows]
    mean_y = sum(target) / len(target)
    ss_res = sum((y - p) ** 2 for y, p in zip(target, pred))
    ss_tot = sum((y - mean_y) ** 2 for y in target)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return beta, r2


# ---------------------------------------------------------------------------
# 数据与真值
# ---------------------------------------------------------------------------
def gen_dataset(spec: dict) -> tuple[list[str], list[list[float]], dict]:
    rng = random.Random(spec["seed"])
    features = spec["features"]
    names = [f[0] for f in features]
    targets = spec["targets"]
    header = ["date", "region"] + names + [t["name"] for t in targets]
    rows: list[list] = []
    regions = ["华东", "华南", "华北", "西南", "华中", "东北"]
    for i in range(spec["n"]):
        values: dict[str, float] = {}
        for name, mean, sd, lo, hi in features:
            values[name] = min(hi, max(lo, rng.gauss(mean, sd)))
        ys = []
        for target in targets:
            y = float(target["intercept"])
            for name in names:
                y += float(target["coefficients"].get(name, 0.0)) * values[name]
            y += rng.gauss(0.0, float(target["noise_sd"]))
            ys.append(round(y, 3))
        date = 20250101 + (i % 365)
        rows.append([date, regions[i % 6]] + [round(values[c], 4) for c in names] + ys)

    matrix = [[1.0] + [row[2 + j] for j in range(len(names))] for row in rows]
    per_target: dict[str, dict] = {}
    flat: dict[str, float] = {}
    r2_values: list[float] = []
    for t_index, target in enumerate(targets):
        beta, r2 = ols(matrix, [row[-len(targets) + t_index] for row in rows])
        coefs = {names[j]: round(beta[1 + j], 4) for j in range(len(names))}
        per_target[target["name"]] = {
            "intercept": round(beta[0], 4),
            "coefficients": coefs,
            "r2": round(r2, 4),
        }
        for key, value in coefs.items():
            flat[f"{target['name']}.{key}"] = value
        r2_values.append(r2)
    recovery = {
        "targets": per_target,
        "coefficients": flat,
        "r2": round(min(r2_values), 4),
        "n": spec["n"],
    }
    return header, rows, recovery


def write_dataset(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# 工作流：15 节点 + 联合对比节点
# ---------------------------------------------------------------------------
def build_workflow(spec: dict) -> dict:
    base = json.loads(BASE_WORKFLOW.read_text(encoding="utf-8"))
    feature_text = "、".join(f[0] for f in spec["features"])
    target_text = "、".join(t["name"] for t in spec["targets"])
    domain_checks = "；".join(spec["domain_checks"])
    marker = f"SCENARIO:{spec['short']}"

    replacements = {
        "wf-ai4s-air-quality": spec["workflow_id"],
        "AI4S 空气质量与气象因子归因分析": spec["title"],
        "examples/datasets/air_quality.csv": spec["dataset"],
        "分析 PM2.5 与温度、湿度、风速、SO2 的定量关系，估计各气象与排放因子的效应系数，并验证可靠性。": spec["task"],
    }
    text = json.dumps(base, ensure_ascii=False)
    for old, new in replacements.items():
        text = text.replace(old, new)
    wf = json.loads(text)
    wf["name"] = spec["title"]
    wf["description"] = spec["description"]
    wf["owner"] = "AgentWeave Demo"
    wf["tags"] = ["demo", "scenario", spec["short"], "multi-target", "causal-attribution"]
    wf["variables"] = {
        "input": {"task": spec["task"], "dataset": spec["dataset"]},
        "dataset_path": spec["dataset"],
        "feedback": "",
        "scenario_id": spec["id"],
        "scenario_marker": marker,
        "scenario_name": spec["name"],
        "targets": [t["name"] for t in spec["targets"]],
        "target_count": len(spec["targets"]),
    }

    prompts = {
        "plan": (
            f"多目标联合分析。研究目标：{{{{input}}}}\\n场景：{spec['name']}（{spec['id']}）\\n"
            f"目标列：{target_text}（共 {len(spec['targets'])} 个，联合分析）\\n背景：{spec['story']}\\n"
            "请拆成 4-6 个任务：分别对每个目标建模、再做联合对比，并给出可验证成功判据。只输出 JSON。"
        ),
        "research": (
            f"场景：{spec['name']}（{spec['id']}），目标列 {target_text}，候选因子：{feature_text}。\\n"
            f"请选择统计方法（多目标多元线性回归 + 稳健性检查），说明需要控制的混杂与数据检查项（{domain_checks}）。只输出 JSON。"
        ),
        "analyze": (
            f"多目标联合分析。场景：{spec['name']}（{spec['id']}），研究标识 {marker}。\\n"
            f"数据集：{{{{dataset_path}}}}，目标列 {target_text}，候选因子：{feature_text}。\\n"
            "请对每个目标分别做多元线性回归，指标用 <目标>.<因子> 扁平键汇总（coefficients、r2、n、p_values）。\\n"
            "写出可独立运行的 Python 复算代码，并输出各目标系数与联合对比结论。\\n"
            "若本轮收到外部校验意见，必须逐项核对并修正，再给出新一轮结论。\\n"
            "{{{{feedback}}}}\\n只输出 JSON。"
        ),
        "segment": (
            f"{marker} 场景：{spec['name']}。目标列 {target_text}，因子：{feature_text}。\\n"
            "请按 2-3 个业务分层维度给出各层样本量、各目标均值与因子均值，并指出分层差异对联合结论的影响。只输出 JSON。"
        ),
        "robustness": (
            f"{marker} 场景：{spec['name']}。目标列 {target_text}。请对多目标模型做留出法稳健性检验："
            "比较前后半样本的系数方向与量级，报告最大相对差异，并判断各目标结论是否稳定。只输出 JSON。"
        ),
        "sensitivity": (
            f"{marker} 场景：{spec['name']}。目标列 {target_text}。请做敏感性分析：逐一剔除候选因子后复算各目标系数，"
            "报告系数最大变化幅度并判断结论对因子选择是否敏感。只输出 JSON。"
        ),
        "joint": (
            f"{marker} 场景：{spec['name']}。目标列 {target_text}，候选因子：{feature_text}。\\n"
            "JOINT_COMPARE 请做多目标联合对比：找出对各目标同向且效应量大的共同抓手，以及方向相反或只对单一目标显著的冲突因子；"
            "给出可用于联合决策的优先级建议。只输出 JSON。"
        ),
        "critic": (
            f"场景：{spec['name']}，目标列 {target_text}。请评审多目标分析过程的证据强度、潜在混杂与结论边界，"
            "给出置信度、风险与改进建议。只输出 JSON。"
        ),
        "report": (
            f"场景：{spec['name']}（{spec['id']}），目标列 {target_text}。请综合各目标结论、联合对比、验证证据与评审意见，"
            "撰写一份中文研究报告（含摘要、数据与方法、各目标发现表、联合结论、验证证据、局限与建议），只输出 JSON。"
        ),
    }

    extra_nodes = [
        {
            "id": "segment",
            "type": "agent",
            "label": "分层画像",
            "description": "按关键维度分组做分层统计，检查结论是否被某一人群/工况主导。",
            "agent": "data_engineer",
            "prompt": prompts["segment"],
            "position": {"x": 750, "y": 430},
        },
        {
            "id": "robustness",
            "type": "agent",
            "label": "稳健性检验",
            "description": "留出法/分半复算，检验各目标系数方向与量级是否稳定。",
            "agent": "statistician",
            "prompt": prompts["robustness"],
            "position": {"x": 1120, "y": 250},
        },
        {
            "id": "sensitivity",
            "type": "agent",
            "label": "敏感性分析",
            "description": "逐一剔除因子复算，观察各目标系数变化。",
            "agent": "statistician",
            "prompt": prompts["sensitivity"],
            "position": {"x": 1120, "y": 560},
        },
        {
            "id": "joint",
            "type": "agent",
            "label": "联合对比",
            "description": "比较各目标的共同驱动与冲突因子，输出联合决策建议。",
            "agent": "statistician",
            "prompt": prompts["joint"],
            "position": {"x": 1350, "y": 405},
        },
    ]
    node_ids = {node["id"] for node in wf["nodes"]}
    for node in extra_nodes:
        if node["id"] not in node_ids:
            wf["nodes"].append(node)

    for node in wf["nodes"]:
        if node["id"] in prompts:
            node["prompt"] = prompts[node["id"]]
        if node["id"] == "scan":
            node["args"] = {"path": spec["dataset"]}
        if node.get("agent"):
            prompt = node.get("prompt") or "请按角色职责完成本节点任务，只输出 JSON。"
            if marker not in prompt:
                node["prompt"] = f"{marker} 场景：{spec['name']}。\\n" + prompt

    wf["edges"] = [e for e in wf["edges"]
                   if not (e.get("source") == "clean" and e.get("target") == "repair")]
    wf["edges"] = [e for e in wf["edges"]
                   if not (e.get("source") == "repair" and e.get("target") == "critique")]
    wf["edges"] += [
        {"source": "clean", "target": "segment", "label": "分层画像"},
        {"source": "segment", "target": "repair", "label": "进入自循环"},
        {"source": "repair", "target": "robustness", "label": "验证通过 · 稳健性"},
        {"source": "repair", "target": "sensitivity", "label": "验证通过 · 敏感性"},
        {"source": "robustness", "target": "joint", "label": "稳健"},
        {"source": "sensitivity", "target": "joint", "label": "敏感"},
        {"source": "joint", "target": "critique", "label": "联合对比"},
    ]
    return wf


def write_readme(spec: dict, recovery: dict) -> str:
    feature_rows = "\\n".join(
        f"| `{name}` | {mean} | {sd} |" for name, mean, sd, *_ in spec["features"]
    )
    target_rows = []
    for target in spec["targets"]:
        coefs = "、".join(f"{k}={v}" for k, v in target["coefficients"].items())
        r2 = recovery["targets"].get(target["name"], {}).get("r2")
        target_rows.append(f"| `{target['name']}` | {target['intercept']} | {coefs} | {r2} |")
    targets_text = "、".join(t["name"] for t in spec["targets"])
    return f"""# {spec['name']} · {spec['title']}

> 场景 ID：`{spec['id']}` · 工作流 ID：`{spec['workflow_id']}` · 数据：`data.csv`（{spec['n']} 行）
> 多目标联合分析：目标数 **{len(spec['targets'])}**，目标列 `{targets_text}`

## 业务问题

{spec['description']}

**背景**：{spec['story']}

## 数据字典

| 字段 | 生成均值 | 生成标准差 |
|---|---|---|
{feature_rows}

| 目标列 | 截距 | 真值系数 | OLS 恢复 R² |
|---|---|---|---|
{chr(10).join(target_rows)}

> 真值仅保存在 `truth.json`（扁平键 `<目标>.<因子>`），平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario {spec['id']}
```

预期：16 个节点执行完成；第 1 轮验证失败（各目标系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；联合对比节点给出共同抓手与冲突因子。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → joint（联合对比）→ critique → gate → report → end`

- 验证为五层：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`、`schema`；
- 多目标指标以 `<目标>.<因子>` 扁平键比对，目标数量不限；
- 首轮/修复轮偏差分别由 `scenario.json` 的 `first_bias` / `second_bias` 控制。

## 本次生成的自检结果

R²（各目标最小值）= {recovery['r2']}；
扁平系数键 {len(recovery['coefficients'])} 个：
```json
{json.dumps(recovery['coefficients'], ensure_ascii=False, indent=2)}
```
"""


def main() -> int:
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    for spec in MULTI_SCENARIOS:
        out_dir = SCENARIOS_DIR / spec["id"]
        header, rows, recovery = gen_dataset(spec)
        write_dataset(out_dir / "data.csv", header, rows)

        targets_truth = {
            t["name"]: {
                "intercept": t["intercept"],
                "coefficients": t["coefficients"],
                "noise_sd": t["noise_sd"],
                "recovery": recovery["targets"].get(t["name"], {}),
            }
            for t in spec["targets"]
        }
        truth = {
            "kind": "multi-target",
            "targets": targets_truth,
            "coefficients": recovery["coefficients"],
            "tolerance": 0.15,
            "statistical_thresholds": {"n_min": 1000, "r2_min": 0.55},
            "n": spec["n"],
            "seed": spec["seed"],
            "ols_recovery": recovery,
            "notes": "隐藏真值：扁平键 <目标>.<因子>，仅供验证器与平台注入，分析 Agent 不应读取。",
        }
        (out_dir / "truth.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        meta = {
            "id": spec["id"],
            "short": spec["short"],
            "kind": "multi-target",
            "name": spec["name"],
            "title": spec["title"],
            "description": spec["description"],
            "dataset": spec["dataset"],
            "targets": [t["name"] for t in spec["targets"]],
            "target": spec["targets"][0]["name"],
            "target_count": len(spec["targets"]),
            "features": [f[0] for f in spec["features"]],
            "workflow_id": spec["workflow_id"],
            "first_bias": spec["first_bias"],
            "second_bias": spec["second_bias"],
            "tolerance": 0.15,
            "statistical_thresholds": {"n_min": 1000, "r2_min": 0.55},
            "task": spec["task"],
            "story": spec["story"],
            "domain_checks": spec["domain_checks"],
            "recovery": recovery,
        }
        (out_dir / "scenario.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        wf = build_workflow(spec)
        (out_dir / "workflow.json").write_text(
            json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out_dir / "README.md").write_text(write_readme(spec, recovery), encoding="utf-8")

        print(f"生成多目标场景：{spec['id']}  目标={meta['targets']}  "
              f"最差R²={recovery['r2']}  键数={len(recovery['coefficients'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
