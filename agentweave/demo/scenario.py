"""演示场景定义：AI4S 空气质量与气象因子归因分析。

为什么要有一个「场景」层
------------------------
平台本身是通用的（任何 Agent 工作流都能跑），但评委需要看到一个**可复现、
有真值、能自证**的完整案例。场景层把「数据在哪、真值是什么、任务怎么描述」
集中到一处，供 CLI、Web、测试、方案书、PPT 共用同一份事实，避免多处硬编码
导致演示与文档不一致。

演示设计要点
------------
* 数据：``examples/datasets/air_quality.csv``，2000 行，固定种子 42 生成；
* 真值：``examples/datasets/air_quality_truth.json`` 中的回归系数，分析 Agent 不可见；
* 首轮：statistician 故意给出偏差系数（wind 误差约 34%），五层验证捕获失败；
* 自循环：验证反馈作为 ``feedback`` 注入第二轮分析，系数修正后通过；
* 这样一次离线运行即可展示「规划 → 并行执行 → 验证 → 失败 → 反馈 → 修复 → 通过」。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCENARIO_ID = "ai4s-air-quality"
WORKFLOW_ID = "wf-ai4s-air-quality"

DATASET_REL = "examples/datasets/air_quality.csv"
TRUTH_REL = "examples/datasets/air_quality_truth.json"

TASK_PROMPT = (
    "分析 2021 年六个城市 2000 条日度观测数据，识别影响 PM2.5 浓度的主要气象与污染物因子，"
    "给出多元线性回归系数、拟合优度与显著性结论，并输出一份可复核的研究报告。"
)

SCENARIO: dict[str, Any] = {
    "id": SCENARIO_ID,
    "workflow_id": WORKFLOW_ID,
    "title": "AI4S · 空气质量归因分析",
    "subtitle": "多智能体协同完成「数据清洗 → 归因建模 → 五层验证 → 自循环修复 → 研究报告」",
    "task": TASK_PROMPT,
    "dataset": DATASET_REL,
    "truth": TRUTH_REL,
    "tags": ["AI4S", "多智能体", "自循环", "五层验证", "可复现"],
    "agents": ["planner", "researcher", "data_engineer", "statistician", "verifier", "critic", "writer"],
    "node_count": 12,
    "expected": {
        "verification_layers": ["code_rerun", "numeric_ground_truth", "statistical", "cross_agent"],
        "min_self_loop_iterations": 1,
        "final_status": "succeeded",
        "tolerance": 0.15,
    },
}

# 首轮故意引入的偏差（与 llm/mock.py 中 MockProvider 的首轮系数一致），
# 用于测试与文档说明，不参与真实计算。
FIRST_ROUND_DEVIATION: dict[str, Any] = {
    "coefficients": {"temp": -0.62, "humidity": 0.30, "wind": -1.05, "so2": 0.19},
    "r2": 0.71,
    "note": "wind 相对真值 -1.6 偏差约 34%，超出 15% 容差，触发验证失败与自循环",
}


def root_dir() -> Path:
    """仓库根目录（本文件位于 ``agentweave/demo/scenario.py``）。"""
    return Path(__file__).resolve().parents[2]


def dataset_path(root: str | Path | None = None) -> Path:
    return Path(root or root_dir()) / DATASET_REL


def truth_path(root: str | Path | None = None) -> Path:
    return Path(root or root_dir()) / TRUTH_REL


def load_truth(root: str | Path | None = None) -> dict[str, Any]:
    path = truth_path(root)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run_request_payload(label: str = "演示运行") -> dict[str, Any]:
    """``POST /api/demo/run`` 与 CLI 共用的请求体。"""
    return {
        "workflow_id": WORKFLOW_ID,
        "scenario": SCENARIO_ID,
        "inputs": {"task": TASK_PROMPT, "dataset": DATASET_REL},
        "policy": "adaptive",
        "label": label,
        "auto_approve": True,
    }


def summary() -> dict[str, Any]:
    """给 Web / 文档用的精简场景卡片。"""
    truth = load_truth()
    return {
        **{key: SCENARIO[key] for key in ("id", "workflow_id", "title", "subtitle", "task", "tags")},
        "dataset": DATASET_REL,
        "rows": truth.get("rows"),
        "ground_truth": truth.get("coefficients", {}),
        "expected": SCENARIO["expected"],
    }


__all__ = [
    "SCENARIO",
    "SCENARIO_ID",
    "WORKFLOW_ID",
    "TASK_PROMPT",
    "FIRST_ROUND_DEVIATION",
    "root_dir",
    "dataset_path",
    "truth_path",
    "load_truth",
    "run_request_payload",
    "summary",
]
