#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成多应用场景演示包（数据 + 真值 + 场景描述 + 工作流）。

用法::

    py -3.14 scripts/generate_scenarios.py

产物（每个场景一个目录，可直接被平台加载）::

    scenarios/<id>/scenario.json   # 场景元数据（目标列、特征、真值、任务描述、首轮偏差）
    scenarios/<id>/data.csv        # 2000 行确定性合成数据
    scenarios/<id>/truth.json      # 隐藏真值（只有验证器/平台可见）
    scenarios/<id>/workflow.json   # 12 节点工作流（JSON DSL，平台自动加载）
    scenarios/<id>/README.md       # 场景说明与运行方式
"""

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
BASE_WORKFLOW = ROOT / "workflows" / "ai4s_air_quality.json"

# ---------------------------------------------------------------------------
# 场景定义：target = intercept + Σ coefficient*feature + N(0, noise)
# feature: (列名, 系数, 均值, 标准差, 下限, 上限)
# ---------------------------------------------------------------------------
SCENARIOS: list[dict] = [
    {
        "id": "finance-sales-attribution",
        "short": "fin-sales",
        "workflow_id": "wf-finance-sales-attribution",
        "name": "零售销售归因",
        "title": "月度销售额与营销因子归因",
        "description": "分析月度销售额（revenue）与广告投放、折扣力度、季节性指数之间的关系，估计因子效应并验证结论。",
        "dataset": "scenarios/finance-sales-attribution/data.csv",
        "target": "revenue",
        "intercept": 120.0,
        "noise_sd": 6.0,
        "seed": 20261101,
        "n": 2000,
        "features": [
            ("ad_spend", 2.40, 25.0, 6.0, 5.0, 50.0),
            ("discount", 0.80, 15.0, 5.0, 2.0, 35.0),
            ("season", 9.00, 0.50, 0.30, 0.0, 1.0),
        ],
        "first_bias": {"ad_spend": 0.310, "discount": 0.290, "season": 0.340},
        "second_bias": {"ad_spend": 0.032, "discount": 0.024, "season": 0.041},
        "task": "分析月度销售额（revenue）与广告投放（ad_spend）、折扣力度（discount）、季节性指数（season）之间的关系，"
                "估计各因子效应系数，并与业务经验对照验证可靠性。",
        "story": "运营团队认为「加大广告投放一定带来同比例销售增长」，需要用数据验证真实弹性，并避免把季节性误判为广告效果。",
        "domain_checks": ["检查量纲与异常值", "控制季节性混杂", "与历史活动做对照"],
    },
    {
        "id": "public-health-risk",
        "short": "pub-health",
        "workflow_id": "wf-public-health-risk",
        "name": "公共卫生风险归因",
        "title": "呼吸道发病率与环境、免疫因子归因",
        "description": "分析地区呼吸道发病率（incidence）与气温、湿度、疫苗接种率、PM2.5 的关系，识别可干预因子并验证结论。",
        "dataset": "scenarios/public-health-risk/data.csv",
        "target": "incidence",
        "intercept": 45.0,
        "noise_sd": 3.1,
        "seed": 20261102,
        "n": 2000,
        "features": [
            ("temp", -0.60, 16.0, 8.0, -10.0, 38.0),
            ("humidity", 0.35, 58.0, 14.0, 20.0, 95.0),
            ("vaccination_rate", -15.00, 0.70, 0.18, 0.30, 0.98),
            ("pm25", 0.20, 42.0, 18.0, 8.0, 160.0),
        ],
        "first_bias": {"temp": 0.290, "humidity": 0.330, "vaccination_rate": 0.310, "pm25": 0.280},
        "second_bias": {"temp": 0.030, "humidity": 0.041, "vaccination_rate": 0.022, "pm25": 0.035},
        "task": "分析地区呼吸道发病率（incidence）与气温（temp）、湿度（humidity）、疫苗接种率（vaccination_rate）、"
                "PM2.5（pm25）的关系，估计各因子效应方向与强度，并判断哪些因子可干预。",
        "story": "疾控部门需要在季节性高发前判断：发病率上升主要是环境驱动还是免疫屏障不足，从而决定资源投向。",
        "domain_checks": ["区分相关与因果", "检查共线性", "关注可干预因子的效应量"],
    },
    {
        "id": "ecommerce-conversion",
        "short": "ecom-conv",
        "workflow_id": "wf-ecommerce-conversion",
        "name": "电商转化归因",
        "title": "页面转化率与体验因子归因",
        "description": "分析页面转化率（conversion）与页面加载速度、个性化推荐强度、购物车步骤数的关系，量化体验改版收益。",
        "dataset": "scenarios/ecommerce-conversion/data.csv",
        "target": "conversion",
        "intercept": 1.80,
        "noise_sd": 0.55,
        "seed": 20261103,
        "n": 2000,
        "features": [
            ("page_speed", 0.060, 72.0, 12.0, 30.0, 100.0),
            ("personalization", 0.900, 0.55, 0.22, 0.0, 1.0),
            ("cart_steps", -0.400, 3.2, 0.9, 1.0, 6.0),
        ],
        "first_bias": {"page_speed": 0.320, "personalization": 0.300, "cart_steps": 0.280},
        "second_bias": {"page_speed": 0.034, "personalization": 0.026, "cart_steps": 0.038},
        "task": "分析页面转化率（conversion，单位 %）与页面速度评分（page_speed）、个性化推荐强度（personalization）、"
                "购物车步骤数（cart_steps）的关系，估计改版各因素对转化的边际影响。",
        "story": "增长团队要决定下一季度把预算投在「提速」「个性化」还是「简化购物车流程」，需要一个可验证的效应排序。",
        "domain_checks": ["确认指标口径", "排查异常流量", "对比改版前后分组"],
    },
    {
        "id": "manufacturing-yield",
        "short": "mfg-yield",
        "workflow_id": "wf-manufacturing-yield",
        "name": "制造良品率归因",
        "title": "产线良品率与工艺参数归因",
        "description": "分析产线良品率（yield_pct）与温度、压力、线速、湿度的关系，定位影响良品率的关键工艺窗口。",
        "dataset": "scenarios/manufacturing-yield/data.csv",
        "target": "yield_pct",
        "intercept": 98.0,
        "noise_sd": 1.8,
        "seed": 20261104,
        "n": 3000,
        "features": [
            ("temp", -0.50, 22.0, 3.0, 15.0, 30.0),
            ("pressure", 2.50, 3.0, 0.4, 1.8, 4.2),
            ("line_speed", -1.20, 12.0, 2.0, 6.0, 18.0),
            ("humidity", -0.12, 45.0, 8.0, 20.0, 70.0),
        ],
        "first_bias": {"temp": 0.300, "pressure": 0.320, "line_speed": 0.280, "humidity": 0.330},
        "second_bias": {"temp": 0.028, "pressure": 0.035, "line_speed": 0.031, "humidity": 0.042},
        "task": "分析产线良品率（yield_pct）与温度（temp）、压力（pressure）、线速（line_speed）、湿度（humidity）的定量关系，"
                "估计各工艺参数的效应系数，并给出可执行的工艺窗口建议。",
        "story": "质量部门连续三周看到良品率波动，但工艺参数每天都在小幅调整，无法判断是哪个参数在起作用，需要可验证的效应排序。",
        "domain_checks": ["核对传感器校准", "区分班次与设备差异", "检查参数共线性"],
    },
    {
        "id": "energy-consumption",
        "short": "energy-kwh",
        "workflow_id": "wf-energy-consumption",
        "name": "工业能耗优化",
        "title": "车间能耗与产量、环境因子归因",
        "description": "分析车间单位能耗（energy_kwh）与产量、气温、夜班比例、设备役龄的关系，识别节能改造优先级。",
        "dataset": "scenarios/energy-consumption/data.csv",
        "target": "energy_kwh",
        "intercept": 120.0,
        "noise_sd": 12.0,
        "seed": 20261105,
        "n": 3000,
        "features": [
            ("output", 1.80, 60.0, 15.0, 10.0, 120.0),
            ("temp", 1.10, 18.0, 8.0, -5.0, 40.0),
            ("night_shift", 18.0, 0.35, 0.48, 0.0, 1.0),
            ("equipment_age", 2.20, 6.0, 3.0, 1.0, 12.0),
        ],
        "first_bias": {"output": 0.310, "temp": 0.290, "night_shift": 0.330, "equipment_age": 0.300},
        "second_bias": {"output": 0.033, "temp": 0.030, "night_shift": 0.040, "equipment_age": 0.027},
        "task": "分析车间单位能耗（energy_kwh）与产量（output）、气温（temp）、夜班比例（night_shift）、设备役龄（equipment_age）的关系，"
                "估计各因子对能耗的边际影响，并排出节能改造优先级。",
        "story": "工厂要在年度预算里选择改造项目：换设备、调班次还是做保温？需要先量化每个因子的能耗贡献，避免拍脑袋。",
        "domain_checks": ["统一能耗口径", "分离产量效应与环境效应", "检查异常工况"],
    },
    {
        "id": "logistics-delivery",
        "short": "logi-deliv",
        "workflow_id": "wf-logistics-delivery",
        "name": "物流时效归因",
        "title": "订单履约时长与距离、负载因子归因",
        "description": "分析订单履约时长（delivery_min）与配送距离、订单量、恶劣天气、仓内负载的关系，支撑时效承诺与资源调度。",
        "dataset": "scenarios/logistics-delivery/data.csv",
        "target": "delivery_min",
        "intercept": 15.0,
        "noise_sd": 4.0,
        "seed": 20261106,
        "n": 3000,
        "features": [
            ("distance_km", 1.50, 8.0, 3.0, 0.3, 40.0),
            ("order_volume", 0.08, 120.0, 40.0, 20.0, 320.0),
            ("bad_weather", 9.0, 0.20, 0.40, 0.0, 1.0),
            ("warehouse_load", 0.09, 70.0, 20.0, 10.0, 130.0),
        ],
        "first_bias": {"distance_km": 0.320, "order_volume": 0.290, "bad_weather": 0.310, "warehouse_load": 0.280},
        "second_bias": {"distance_km": 0.032, "order_volume": 0.027, "bad_weather": 0.038, "warehouse_load": 0.030},
        "task": "分析订单履约时长（delivery_min）与配送距离（distance_km）、订单量（order_volume）、恶劣天气（bad_weather）、"
                "仓内负载（warehouse_load）的关系，量化各因子对时效的影响并给出调度建议。",
        "story": "平台承诺「次日达」，但履约时长波动大；运营需要知道哪些因子可控（仓内负载 vs 天气），以及距离的边际成本。",
        "domain_checks": ["排除异常大单", "区分天气与节假日", "核对仓内负载口径"],
    },
    {
        "id": "education-performance",
        "short": "edu-score",
        "workflow_id": "wf-education-performance",
        "name": "教学成效归因",
        "title": "学生成绩与学习行为因子归因",
        "description": "分析期末成绩（final_score）与出勤率、作业完成度、自习时长、班级规模的关系，为教学干预提供证据。",
        "dataset": "scenarios/education-performance/data.csv",
        "target": "final_score",
        "intercept": 40.0,
        "noise_sd": 5.0,
        "seed": 20261107,
        "n": 3000,
        "features": [
            ("attendance", 0.45, 85.0, 8.0, 40.0, 100.0),
            ("homework", 0.30, 75.0, 12.0, 20.0, 100.0),
            ("study_hours", 1.60, 8.0, 2.5, 1.0, 20.0),
            ("class_size", -0.12, 40.0, 8.0, 15.0, 65.0),
        ],
        "first_bias": {"attendance": 0.280, "homework": 0.310, "study_hours": 0.330, "class_size": 0.300},
        "second_bias": {"attendance": 0.026, "homework": 0.031, "study_hours": 0.040, "class_size": 0.029},
        "task": "分析学生期末成绩（final_score）与出勤率（attendance）、作业完成度（homework）、自习时长（study_hours）、"
                "班级规模（class_size）的关系，估计各因子效应并给出教学干预建议。",
        "story": "学校要设计下学期干预方案：抓出勤、抓作业还是增加自习？需要可解释、可复算的效应量，而不是经验判断。",
        "domain_checks": ["检查样本代表性", "区分班级与个体效应", "避免把相关当因果"],
    },
    {
        "id": "marketing-churn",
        "short": "mkt-churn",
        "workflow_id": "wf-marketing-churn",
        "name": "用户流失归因",
        "title": "订阅用户流失与满意度、投诉因子归因",
        "description": "分析月度流失率（churn_pct）与满意度、投诉量、在网时长、价格指数的关系，识别留存干预的抓手。",
        "dataset": "scenarios/marketing-churn/data.csv",
        "target": "churn_pct",
        "intercept": 35.0,
        "noise_sd": 2.2,
        "seed": 20261108,
        "n": 3000,
        "features": [
            ("satisfaction", -0.25, 70.0, 12.0, 20.0, 100.0),
            ("complaints", 0.08, 30.0, 15.0, 0.0, 90.0),
            ("tenure", -0.15, 24.0, 10.0, 1.0, 60.0),
            ("price_index", 0.12, 100.0, 10.0, 70.0, 140.0),
        ],
        "first_bias": {"satisfaction": 0.330, "complaints": 0.290, "tenure": 0.310, "price_index": 0.280},
        "second_bias": {"satisfaction": 0.040, "complaints": 0.027, "tenure": 0.032, "price_index": 0.030},
        "task": "分析订阅用户月度流失率（churn_pct）与满意度（satisfaction）、投诉量（complaints）、在网时长（tenure）、"
                "价格指数（price_index）的关系，量化各因子影响并给出留存优先级。",
        "story": "留存团队面对预算约束：降价、补贴客服还是做满意度项目？需要先确认哪个因子对流失的边际贡献最大。",
        "domain_checks": ["核对流失口径", "排除一次性促销", "控制用户结构差异"],
    },
]


# ---------------------------------------------------------------------------
# 数值工具（标准库实现 OLS，用于自检）
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
# 数据生成
# ---------------------------------------------------------------------------
def gen_dataset(spec: dict) -> tuple[list[str], list[list[float]], dict]:
    rng = random.Random(spec["seed"])
    features = spec["features"]
    cols = [f[0] for f in features]
    header = ["date", "region"] + cols + [spec["target"]]
    rows: list[list] = []
    regions = ["华东", "华南", "华北", "西南", "华中", "东北"]
    for i in range(spec["n"]):
        values = {}
        for name, coef, mean, sd, lo, hi in features:
            v = rng.gauss(mean, sd)
            values[name] = min(hi, max(lo, v))
        y = spec["intercept"]
        for name, coef, *_ in features:
            y += coef * values[name]
        y += rng.gauss(0, spec["noise_sd"])
        date = 20250101 + (i % 365)
        rows.append([date, regions[i % 6]] + [round(values[c], 4) for c in cols] + [round(y, 3)])
    # OLS 自检（用真实列）
    matrix = [[1.0] + [row[2 + j] for j in range(len(cols))] for row in rows]
    beta, r2 = ols(matrix, [row[-1] for row in rows])
    recovery = {
        "intercept": round(beta[0], 4),
        "coefficients": {cols[j]: round(beta[1 + j], 4) for j in range(len(cols))},
        "r2": round(r2, 4),
    }
    return header, rows, recovery


def write_dataset(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# 工作流生成（基于默认 12 节点模板，替换场景文本与数据路径）
# ---------------------------------------------------------------------------
def build_workflow(spec: dict) -> dict:
    base = json.loads(BASE_WORKFLOW.read_text(encoding="utf-8"))
    feature_text = "、".join(f"{f[0]}" for f in spec["features"])
    domain_checks = "；".join(spec["domain_checks"])

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
    wf["tags"] = ["demo", "scenario", spec["short"], "causal-attribution"]
    wf["variables"] = {
        "input": {"task": spec["task"], "dataset": spec["dataset"]},
        "dataset_path": spec["dataset"],
        "feedback": "",
        "scenario_id": spec["id"],
        "scenario_marker": f"SCENARIO:{spec['short']}",
        "scenario_name": spec["name"],
    }

    prompts = {
        "plan": (
            f"研究目标：{{{{input}}}}\n场景：{spec['name']}（{spec['id']}）\n背景：{spec['story']}\n"
            "请把目标拆成 3-6 个可执行任务，并为每个任务指定执行者（data_engineer/statistician/researcher/writer/verifier），"
            "给出可验证的成功判据。只输出 JSON。"
        ),
        "research": (
            f"场景：{spec['name']}（{spec['id']}），目标变量 {spec['target']}，候选因子：{feature_text}。\n"
            f"请选择统计方法（多元线性回归 + 稳健性检查），说明需要控制的混杂与数据检查项（{domain_checks}），并给出理由。只输出 JSON。"
        ),
        "analyze": (
            f"场景：{spec['name']}（{spec['id']}），研究标识 SCENARIO:{spec['short']}。\n"
            f"数据集：{{{{dataset_path}}}}，目标列 {spec['target']}，候选因子：{feature_text}。\n"
            "请用多元线性回归估计各因子系数、R²、样本量与显著性，并写出可独立运行的 Python 复算代码。\n"
            "若本轮收到外部校验意见，必须逐项核对并修正，再给出新一轮结论。\n"
            "{{{{feedback}}}}\n只输出 JSON。"
        ),
        "critic": (
            f"场景：{spec['name']}，目标 {spec['target']}。请评审分析过程的证据强度、潜在混杂与结论边界，"
            "给出置信度、风险与改进建议。只输出 JSON。"
        ),
        "report": (
            f"场景：{spec['name']}（{spec['id']}），目标 {spec['target']}。请综合分析结论、验证证据与评审意见，"
            "撰写一份中文研究报告（含摘要、数据与方法、主要发现表、验证证据、局限与建议），只输出 JSON。"
        ),
    }
    marker = f"SCENARIO:{spec['short']}"
    for node in wf["nodes"]:
        if node["id"] in prompts:
            node["prompt"] = prompts[node["id"]]
        if node["id"] == "scan":
            node["args"] = {"path": spec["dataset"]}
        # 所有 Agent 节点都带上场景标记：Mock 提供方据此切换到对应场景，
        # 尤其是 verify 节点（其提示词默认为空，否则会走回默认空气质量逻辑）。
        if node.get("agent"):
            prompt = node.get("prompt") or "请按角色职责完成本节点任务，只输出 JSON。"
            if marker not in prompt:
                node["prompt"] = f"{marker} 场景：{spec['name']}。\n" + prompt

    # ---- 让编排更复杂：在 12 节点基础上增加分层画像、稳健性、敏感性三个节点 ----
    extra_nodes = [
        {
            "id": "segment",
            "type": "agent",
            "label": "分层画像",
            "description": "按关键维度分组做分层统计，检查整体结论是否被某一人群/工况主导。",
            "agent": "data_engineer",
            "prompt": (
                f"{marker} 场景：{spec['name']}。目标 {spec['target']}，因子：{feature_text}。\n"
                "请按 2-3 个业务分层维度给出各层样本量、目标均值与因子均值，并指出各层差异是否可能影响总体结论。只输出 JSON。"
            ),
            "position": {"x": 750, "y": 400},
        },
        {
            "id": "robustness",
            "type": "agent",
            "label": "稳健性检验",
            "description": "留出法/分半复算，检验系数方向与量级是否稳定。",
            "agent": "statistician",
            "prompt": (
                f"{marker} 场景：{spec['name']}。请对主模型做留出法稳健性检验：比较前后半样本的系数方向与量级，"
                "报告最大相对差异，并给出结论是否稳定的判断。只输出 JSON。"
            ),
            "position": {"x": 1300, "y": 250},
        },
        {
            "id": "sensitivity",
            "type": "agent",
            "label": "敏感性分析",
            "description": "逐一剔除因子复算，观察其余系数变化。",
            "agent": "statistician",
            "prompt": (
                f"{marker} 场景：{spec['name']}。请做敏感性分析：逐一剔除候选因子后复算其余系数，"
                "报告系数最大变化幅度并判断结论对因子选择是否敏感。只输出 JSON。"
            ),
            "position": {"x": 1300, "y": 520},
        },
    ]
    node_ids = {node["id"] for node in wf["nodes"]}
    for node in extra_nodes:
        if node["id"] not in node_ids:
            wf["nodes"].append(node)

    def replace_edge(src: str, dst: str, label: str) -> None:
        for edge in wf["edges"]:
            if edge.get("source") == src and edge.get("target") == dst:
                edge["label"] = label
                return

    # clean -> segment -> repair；repair 退出后并行 robustness/sensitivity -> critique
    wf["edges"] = [e for e in wf["edges"] if not (e.get("source") == "clean" and e.get("target") == "repair")]
    wf["edges"] = [e for e in wf["edges"] if not (e.get("source") == "repair" and e.get("target") == "critique")]
    wf["edges"] += [
        {"source": "clean", "target": "segment", "label": "分层画像"},
        {"source": "segment", "target": "repair", "label": "进入自循环"},
        {"source": "repair", "target": "robustness", "label": "验证通过 · 稳健性"},
        {"source": "repair", "target": "sensitivity", "label": "验证通过 · 敏感性"},
        {"source": "robustness", "target": "critique", "label": "稳健"},
        {"source": "sensitivity", "target": "critique", "label": "敏感"},
    ]
    return wf


def write_readme(spec: dict, recovery: dict) -> str:
    features = "\n".join(
        f"| `{name}` | {coef} | {mean} | {sd} |" for name, coef, mean, sd, *_ in spec["features"]
    )
    return f"""# {spec['name']} · {spec['title']}

> 场景 ID：`{spec['id']}` · 工作流 ID：`{spec['workflow_id']}` · 数据：`data.csv`（{spec['n']} 行）

## 业务问题

{spec['description']}

**背景**：{spec['story']}

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
{features}
| `{spec['target']}` | — | 因变量：{spec['intercept']} + Σ 系数×因子 + N(0, {spec['noise_sd']}) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario {spec['id']}
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario {spec['id']} --json
```

本次生成时的 OLS 恢复结果：R² = {recovery['r2']}，系数 {json.dumps(recovery['coefficients'], ensure_ascii=False)}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
"""


def main() -> int:
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for spec in SCENARIOS:
        out_dir = SCENARIOS_DIR / spec["id"]
        header, rows, recovery = gen_dataset(spec)
        write_dataset(out_dir / "data.csv", header, rows)

        truth = {
            "intercept": spec["intercept"],
            "coefficients": {name: coef for name, coef, *_ in spec["features"]},
            "noise_sd": spec["noise_sd"],
            "tolerance": 0.15,
            "statistical_thresholds": {"n_min": 1000, "r2_min": 0.55},
            "seed": spec["seed"],
            "n": spec["n"],
            "target": spec["target"],
            "ols_recovery": recovery,
            "notes": "隐藏真值：仅供验证器与平台注入，分析 Agent 不应读取。",
        }
        (out_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        meta = {
            "id": spec["id"],
            "short": spec["short"],
            "name": spec["name"],
            "title": spec["title"],
            "description": spec["description"],
            "dataset": spec["dataset"],
            "target": spec["target"],
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
        (out_dir / "scenario.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        wf = build_workflow(spec)
        (out_dir / "workflow.json").write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        (out_dir / "README.md").write_text(write_readme(spec, recovery), encoding="utf-8")
        index_rows.append((spec, recovery))
        print(f"生成场景：{spec['id']}  R²={recovery['r2']}  系数={recovery['coefficients']}")

    index = ["# 多应用场景演示\n",
             "> 同一套 AgentWeave 引擎，换数据与工作流即可复用到不同行业。以下场景均可离线运行。\n",
             "| # | 场景 | 目录 | 工作流 | 一键运行 |", "|---|---|---|---|---|"]
    for i, (spec, _rec) in enumerate(index_rows, 1):
        index.append(f"| {i} | {spec['name']} | `scenarios/{spec['id']}/` | `{spec['workflow_id']}` | "
                     f"`py -3.14 scripts/run_scenario.py --scenario {spec['id']}` |")
    index += [
        "\n## 共同点\n",
        "- 每个场景都是 15 节点工作流：并行取数/调研 → 数据工程 → 分层画像 → 统计建模 → 五层验证 → PDCA 自循环 → 稳健性/敏感性 → 评审 → 报告；",
        "- 每个场景的合成数据都内置真实系数与首轮偏差，因此离线可复现「首轮失败 → 反馈修复 → 第二轮通过」；",
        "- 真值只在 `truth.json`，由平台注入验证器；分析 Agent 的上下文里没有真值；",
        "- 换场景只换数据与工作流 JSON，引擎、调度、验证、Web 控制台均不改。\n",
        "## Web 控制台\n",
        "`py -3.14 server.py` 启动后，编排台下拉框会列出全部场景工作流，可直接选择运行。\n",
    ]
    (SCENARIOS_DIR / "README.md").write_text("\n".join(index), encoding="utf-8")
    print(f"\n场景索引：{SCENARIOS_DIR / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
