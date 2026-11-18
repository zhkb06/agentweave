"""确定性 Mock LLM —— 离线演示 / 单元测试专用，零网络、可复现。

设计目标
--------
1. **完全确定性**：同一输入（messages）永远返回同一输出；``seed`` 只作为
   可复现性声明，不引入随机噪声，保证演示与测试稳定。
2. **按角色出结果**：通过 system prompt 中的 ``role=<role>`` 元信息或角色
   关键词识别 planner / researcher / data_engineer / statistician /
   verifier / critic / writer，输出与 docs/CONTRACTS.md §5 及 demo 场景
   完全一致的结构化 JSON（顶层 summary / evidence / confidence + 角色字段）。
3. **驱动自循环**：statistician 首轮故意给出与真值偏差 25%~37% 的系数，
   当 prompt 中出现 feedback / 修复 / 偏差 / 请重新 等关键信号时改为
   修正后的系数（与真实 OLS 复算误差 ≤7%），从而离线演示
   “验证失败 → 反馈注入 → 修复 → 通过”的完整闭环。
4. **真实可运行的分析代码**：statistician 返回的 ``code`` 是纯标准库 OLS
   脚本（读取 ``examples/datasets/air_quality.csv``），首轮/修复轮返回同一段
   真实代码——因此 code_rerun 检查器首轮能发现“申报系数 ≠ 复算系数”，
   修复轮则一致，这是演示可信度的核心证据。

Mock 也支持 ReAct：当提供工具且尚未出现工具观察时，statistician / verifier
会先返回 ``{"thought": ..., "tool": "python_exec", "args": {...}}`` 工具调用，
运行时会执行工具、回注观察，再次调用时才输出最终 JSON。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import stats as _stats
from .base import LLMProvider, LLMResult

#: 演示数据集中的“真实”系数（由 Lead 用固定种子生成并实测恢复）。
TRUTH_INTERCEPT = 78.0
TRUTH_COEFFICIENTS: dict[str, float] = {
    "temp": -0.8439,
    "humidity": 0.4375,
    "wind": -1.6728,
    "so2": 0.2577,
}
TRUTH_R2 = 0.7138
TRUTH_N = 2000

FEATURE_NAMES: tuple[str, ...] = ("temp", "humidity", "wind", "so2")

#: 首轮（未修复）故意偏差的系数申报。
FIRST_ROUND_COEFFICIENTS: dict[str, float] = {
    "temp": -0.62,
    "humidity": 0.30,
    "wind": -1.05,
    "so2": 0.19,
}
FIRST_ROUND_R2 = 0.71
FIRST_ROUND_P_VALUES: dict[str, float] = {
    "temp": 4.2e-09,
    "humidity": 1.7e-11,
    "wind": 2.3e-13,
    "so2": 8.5e-08,
}

#: 修复轮（收到验证反馈后）系数，与真实 OLS 复算误差 ≤7%。
FIXED_COEFFICIENTS: dict[str, float] = {
    "temp": -0.84,
    "humidity": 0.44,
    "wind": -1.67,
    "so2": 0.26,
}
FIXED_ROUND_R2 = 0.72
FIXED_ROUND_P_VALUES: dict[str, float] = {
    "temp": 1.1e-09,
    "humidity": 3.6e-12,
    "wind": 7.8e-14,
    "so2": 2.4e-08,
}

DEFAULT_DATASET_PATH = "examples/datasets/air_quality.csv"

#: 修复轮关键字（收到验证反馈后进入“修复模式”）。
_REPAIR_MARKERS: tuple[str, ...] = (
    "【上一轮验证反馈】",
    "feedback",
    "修复",
    "偏差",
    "请重新",
    "重新拟合",
    "验证失败",
)

_ROLE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("verifier", ("独立验证", "验证专家", "独立复算", "交叉验证")),
    ("critic", ("评审专家", "科研评审", "评审者")),
    ("writer", ("报告撰写", "撰写专家", "科技写作")),
    ("statistician", ("统计建模", "统计建模专家", "多元线性回归", "回归建模")),
    ("data_engineer", ("数据工程", "数据质量", "清洗")),
    ("researcher", ("文献", "调研", "方法调研")),
    ("planner", ("任务规划", "规划专家", "任务分解")),
)

#: python_exec 中真实可运行的 OLS 分析脚本（纯标准库）。
_REGRESSION_CODE_TEMPLATE = '''# -*- coding: utf-8 -*-
"""多元线性回归（普通最小二乘，纯标准库实现，可直接运行）。"""
import csv
import json
import math
import os

FEATURES = ["temp", "humidity", "wind", "so2"]


def locate(path):
    """在仓库根目录 / 运行产物目录中定位数据文件。"""
    candidates = [
        path,
        os.path.join(os.environ.get("AGENTWEAVE_WORKSPACE", ""), path),
        os.path.join(os.environ.get("AGENTWEAVE_ARTIFACTS", ""), path),
        os.path.join("examples", "datasets", "air_quality.csv"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError("找不到数据文件: %s" % path)


def load_rows(path):
    """读取 CSV 并转成数值行。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            rows.append({key: float(record[key]) for key in ["pm25"] + FEATURES})
    return rows


def solve(matrix, vector):
    """列主元高斯消元求解线性方程组。"""
    n = len(vector)
    aug = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        if abs(aug[col][col]) < 1e-12:
            raise ValueError("设计矩阵奇异，无法求逆")
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for k in range(col, n + 1):
                aug[row][k] -= factor * aug[col][k]
    beta = [0.0] * n
    for i in range(n - 1, -1, -1):
        beta[i] = (aug[i][n] - sum(aug[i][j] * beta[j] for j in range(i + 1, n))) / aug[i][i]
    return beta


def invert(matrix):
    """高斯-约旦消元求逆矩阵（用于系数标准误）。"""
    n = len(matrix)
    aug = [list(matrix[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        if abs(aug[col][col]) < 1e-12:
            raise ValueError("设计矩阵奇异，无法求逆")
        divisor = aug[col][col]
        aug[col] = [value / divisor for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            aug[row] = [aug[row][k] - factor * aug[col][k] for k in range(2 * n)]
    return [row[n:] for row in aug]


def main():
    path = locate("__DATA_PATH__")
    rows = load_rows(path)
    n = len(rows)
    y = [row["pm25"] for row in rows]
    x = [[row[feature] for feature in FEATURES] for row in rows]
    k = len(FEATURES) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        design = [1.0] + x[i]
        for a in range(k):
            xty[a] += design[a] * y[i]
            for b in range(k):
                xtx[a][b] += design[a] * design[b]
    beta = solve(xtx, xty)
    fitted = [sum(beta[j] * ([1.0] + x[i])[j] for j in range(k)) for i in range(n)]
    y_mean = sum(y) / n
    ss_tot = sum((value - y_mean) ** 2 for value in y)
    ss_res = sum((y[i] - fitted[i]) ** 2 for i in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    inv = invert(xtx)
    sigma2 = ss_res / max(n - k, 1)
    p_values = {}
    for index, feature in enumerate(FEATURES, start=1):
        se = math.sqrt(max(sigma2 * inv[index][index], 0.0))
        t_value = beta[index] / se if se else 0.0
        p_values[feature] = math.erfc(abs(t_value) / math.sqrt(2.0))
    coefficients = {"intercept": beta[0]}
    for index, feature in enumerate(FEATURES, start=1):
        coefficients[feature] = beta[index]
    print(json.dumps({"coefficients": coefficients, "r2": r2, "n": n, "p_values": p_values}, ensure_ascii=False))


main()
'''


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按字符、英文按 4 字符≈1 token）。"""
    if not text:
        return 0
    return max(1, len(text) // 3)


class MockProvider(LLMProvider):
    """确定性、零网络的 LLM 提供方（构造参数全部可选）。"""

    name = "mock"

    def __init__(
        self,
        seed: int = 42,
        model: str = "mock-deterministic",
        latency_s: float = 0.002,
    ) -> None:
        self.seed = int(seed)
        self.model = model or "mock-deterministic"
        self.latency_s = max(0.0, float(latency_s))

    # ------------------------------------------------------------------
    # LLMProvider 接口
    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: str | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        """根据消息内容确定性地生成一段 JSON 文本。"""
        started = time.perf_counter()
        role = self._detect_role(messages)
        text = self._all_text(messages)
        feedback = self._extract_feedback(messages)
        repair = bool(feedback.strip()) or self._is_repair(text)
        has_observation = self._has_tool_observation(messages)

        if role == "planner":
            content = self._planner(text)
        elif role == "researcher":
            content = self._researcher(text)
        elif role == "data_engineer":
            content = self._data_engineer(text)
        elif role == "statistician":
            content = self._statistician(messages, bool(tools), has_observation, repair)
        elif role == "verifier":
            content = self._verifier(messages, text, bool(tools), has_observation)
        elif role == "critic":
            content = self._critic(text)
        elif role == "writer":
            content = self._writer(text)
        else:
            content = self._generic(text)

        return LLMResult(
            content=content,
            tool_calls=[],
            tokens_in=sum(estimate_tokens(str(m.get("content", ""))) for m in messages),
            tokens_out=estimate_tokens(content),
            model=self.model,
            latency_s=max(time.perf_counter() - started, self.latency_s),
            raw={"mock": True, "role": role, "repair": repair, "seed": self.seed},
            provider=self.name,
        )

    async def list_models(self) -> list[str]:
        return ["mock-deterministic", "mock-fast", "mock-smart"]

    # ------------------------------------------------------------------
    # 角色 / 状态识别
    # ------------------------------------------------------------------
    @staticmethod
    def _all_text(messages: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for message in messages:
            if isinstance(message, dict):
                parts.append(str(message.get("content", "")))
        return "\n".join(parts)

    @staticmethod
    def _extract_feedback(messages: list[dict[str, str]]) -> str:
        """提取运行时注入的“上一轮验证反馈”原文。"""
        marker = "【上一轮验证反馈】"
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "user":
                continue
            content = str(message.get("content", ""))
            if marker in content:
                return content.split(marker, 1)[1]
        return ""

    @classmethod
    def _is_repair(cls, text: str) -> bool:
        """根据关键字判断是否处于“收到反馈后的修复轮”。"""
        stripped = text.split("【上一轮验证反馈】", 1)[0] if "【上一轮验证反馈】" in text else text
        # 反馈区本身存在时按修复轮处理（由 _extract_feedback 负责）；
        # 这里主要覆盖外部调用方直接把反馈拼进 prompt 的情况。
        lowered = stripped.lower()
        return any(marker.lower() in lowered for marker in _REPAIR_MARKERS)

    @staticmethod
    def _detect_role(messages: list[dict[str, str]]) -> str:
        """识别 Agent 角色：优先读 ``role=xxx`` 元信息，再按系统提示关键词。"""
        system_text = ""
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                system_text += str(message.get("content", "")) + "\n"
        match = re.search(r"role\s*=\s*([a-zA-Z_]+)", system_text)
        if match:
            role = match.group(1).lower()
            if role in {name for name, _ in _ROLE_KEYWORDS}:
                return role
        for role, keywords in _ROLE_KEYWORDS:
            if any(keyword in system_text for keyword in keywords):
                return role
        return "generic"

    @staticmethod
    def _has_tool_observation(messages: list[dict[str, str]]) -> bool:
        return any(isinstance(message, dict) and message.get("role") == "tool" for message in messages)

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    @staticmethod
    def _json(data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _dataset_path(messages: list[dict[str, str]]) -> str:
        """从消息中提取 CSV 数据集路径（优先最近的、显式的路径）。"""
        text = MockProvider._all_text(messages)
        text = text.split("【上一轮验证反馈】", 1)[0]
        matches = re.findall(r"[^\s\"'`,;，。()\[\]{}<>]+\.csv", text)
        cleaned: list[str] = []
        for item in matches:
            value = item.strip().strip("\\/")
            value = value.replace("\\\\", "\\")
            if value and value.lower().endswith(".csv"):
                cleaned.append(value)
        if not cleaned:
            return DEFAULT_DATASET_PATH
        # 去掉可能残留的转义反斜杠，取最后一个（最贴近当前任务的数据文件）
        return cleaned[-1]

    @classmethod
    def _regression_code(cls, messages: list[dict[str, str]]) -> str:
        path = cls._dataset_path(messages).replace("\\", "\\\\").replace('"', '\\"')
        return _REGRESSION_CODE_TEMPLATE.replace("__DATA_PATH__", path)

    @staticmethod
    def _tool_call(thought: str, tool: str, args: dict[str, Any]) -> str:
        return json.dumps({"thought": thought, "tool": tool, "args": args}, ensure_ascii=False)

    @staticmethod
    def _user_text(messages: list[dict[str, str]]) -> str:
        """只拼接 system/user 消息（排除工具观察与工具调用参数）。"""
        parts: list[str] = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") in ("system", "user"):
                parts.append(str(message.get("content", "")))
        return "\n".join(parts)

    @staticmethod
    def _claimed_coefficients(text: str) -> dict[str, float]:
        """从上下文 JSON 中解析申报系数（取最后一个 coefficients 块）。"""
        text = text.split("【上一轮验证反馈】", 1)[0]
        blocks = re.findall(r'"coefficients"\s*:\s*\{([^{}]*)\}', text)
        source = blocks[-1] if blocks else text
        claimed: dict[str, float] = {}
        for factor in FEATURE_NAMES:
            matches = re.findall(r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)' % factor, source)
            if matches:
                claimed[factor] = float(matches[-1])
        return claimed

    @staticmethod
    def _claimed_number(text: str, key: str) -> float | None:
        matches = re.findall(r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)' % re.escape(key), text)
        return float(matches[-1]) if matches else None

    @staticmethod
    def _closest(values: list[float], target: float, default: float) -> float:
        if not values:
            return default
        return min(values, key=lambda value: abs(value - target))

    @classmethod
    def _pick_value(cls, text: str, factor: str, target: float) -> float:
        matches = re.findall(r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)' % factor, text)
        values = [float(item) for item in matches]
        return cls._closest(values, target, target)

    # ------------------------------------------------------------------
    # 各角色输出
    # ------------------------------------------------------------------
    def _planner(self, text: str) -> str:
        tasks = [
            {"id": "scan", "goal": "读取并统计空气质量数据集，输出行列数、缺失值与预览", "owner": "data_engineer"},
            {"id": "research", "goal": "调研 PM2.5 归因的统计建模范式与检验方法", "owner": "researcher"},
            {"id": "clean", "goal": "完成数据质量检查与清洗，产出可分析数据文件", "owner": "data_engineer"},
            {"id": "analyze", "goal": "用多元线性回归估计气象/污染物因子系数、R²、p 值", "owner": "statistician"},
            {"id": "verify", "goal": "独立复算关键系数并与真值、申报值比对", "owner": "verifier"},
            {"id": "report", "goal": "把已验证的指标与证据组织成中文研究报告", "owner": "writer"},
        ]
        criteria = [
            "wind 系数相对真值相对误差 ≤ 15%",
            "模型拟合优度 R² ≥ 0.55",
            "有效样本量 n ≥ 1000",
            "报告必须包含验证证据、局限与改进建议",
        ]
        return self._json(
            {
                "summary": "已将“空气质量与气象因子归因”拆解为 6 个可执行子任务，并给出 4 条量化成功判据。",
                "tasks": tasks,
                "success_criteria": criteria,
                "evidence": ["任务覆盖数据读取、方法调研、清洗、建模、验证与报告全链路"],
                "confidence": 0.9,
            }
        )

    def _researcher(self, text: str) -> str:
        methods = [
            "多元线性回归（普通最小二乘 OLS）：估计各气象/污染物因子对 PM2.5 的边际效应",
            "OLS 假设检验：用系数标准误与 t 检验给出 p 值，判断效应显著性",
            "多重共线性与稳健性检查：相关矩阵 / VIF，必要时做变量筛选或正则化对照",
            "交叉验证与残差诊断：检查拟合优度 R²、残差正态性与异方差迹象",
        ]
        rationale = (
            "PM2.5 与温度、湿度、风速、SO₂ 之间近似线性可加关系，且样本量充足（n=2000），"
            "多元线性回归可解释性强、可用标准库精确复算，适合作为可验证的基线模型；"
            "通过独立复算与阈值检查，可避免只依赖单一模型自评。"
        )
        return self._json(
            {
                "summary": "建议采用多元线性回归作为主方法，并配合显著性、共线性与交叉验证三类检查。",
                "methods": methods,
                "rationale": rationale,
                "evidence": ["标准库即可实现 OLS 与 t 检验，满足离线可复现要求"],
                "confidence": 0.85,
            }
        )

    def _data_engineer(self, text: str) -> str:
        data_path = self._dataset_path([] if not text else [{"role": "user", "content": text}])
        return self._json(
            {
                "summary": "数据质量检查完成：2000 行 8 列，无缺失值，数值字段量纲正常，可直接进入回归分析。",
                "data_path": data_path,
                "rows": TRUTH_N,
                "cleaning_log": [
                    "读取 CSV 并校验 8 个字段：date, city, pm25, temp, humidity, wind, so2, no2",
                    "检查缺失值：各列缺失数均为 0，无需插补",
                    "检查异常值：PM2.5 与风速落在合理区间，未发现明显录入错误",
                    "统一数值字段类型为 float，保留日期与城市字段用于分组统计",
                ],
                "evidence": ["dataset_stats：shape=(2000, 8)，missing=0"],
                "confidence": 0.92,
            }
        )

    def _statistician(self, messages: list[dict[str, Any]], has_tools: bool, has_observation: bool, repair: bool) -> str:
        code = self._regression_code(messages)
        # ReAct：第一轮先真实运行代码，拿到工具观察后再输出最终结论。
        if has_tools and not has_observation:
            return self._tool_call(
                "需要先通过 python_exec 真实运行 OLS 脚本，确认可复现的系数后再汇报。",
                "python_exec",
                {"code": code, "timeout_s": 10},
            )

        if repair:
            coefficients = dict(FIXED_COEFFICIENTS)
            r2 = FIXED_ROUND_R2
            p_values = dict(FIXED_ROUND_P_VALUES)
            conclusion = (
                "已根据验证反馈用最小二乘重新拟合：风速每增加 1 个单位，PM2.5 平均下降约 1.67；"
                "温度、湿度与 SO₂ 的估计同样显著，模型 R²≈0.72、n=2000。"
            )
            summary = "已按验证反馈重新拟合：wind 系数修正为 -1.67（R²=0.72，n=2000），与独立复算一致。"
            confidence = 0.88
            evidence = [
                "python_exec 真实运行 OLS 脚本，stdout 返回 n=2000、r2≈0.71",
                "已针对 verifier 指出的 wind/temp/humidity/so2 偏差重新拟合",
                "四个系数 p 值均小于 0.01，统计显著",
            ]
        else:
            coefficients = dict(FIRST_ROUND_COEFFICIENTS)
            r2 = FIRST_ROUND_R2
            p_values = dict(FIRST_ROUND_P_VALUES)
            conclusion = (
                "四个因子均显著：风速每增加 1 个单位，PM2.5 平均下降约 1.05；"
                "温度与湿度分别呈负、正效应，SO₂ 呈正向效应，模型 R²≈0.71、n=2000。"
            )
            summary = "多元回归完成：wind 系数估计为 -1.05（R²=0.71，n=2000），四个因子均显著。"
            confidence = 0.62
            evidence = [
                "python_exec 真实运行 OLS 脚本，stdout 返回 n=2000",
                "系数估计基于普通最小二乘，p 值均小于 0.01",
            ]

        metrics = {
            "coefficients": coefficients,
            "r2": r2,
            "n": TRUTH_N,
            "p_values": p_values,
            "conclusion": conclusion,
        }
        try:
            fit = _stats.fit(DEFAULT_DATASET_PATH, "pm25",
                             ["temp", "humidity", "wind", "so2"])
            metrics.update({
                "r2": fit["r2"], "r2_holdout": fit["r2_holdout"],
                "n": fit["n"], "p_values": fit["p_values"],
                "ci95": fit["ci95"], "vif": fit["vif"],
                "data_quality": fit["data_quality"],
            })
        except Exception:  # pragma: no cover - 数据缺失时保持基础指标
            pass
        return self._json(
            {
                "summary": summary,
                "code": code,
                "metrics": metrics,
                "evidence": evidence,
                "confidence": confidence,
            }
        )

    def _verifier(self, messages: list[dict[str, Any]], text: str, has_tools: bool, has_observation: bool) -> str:
        code = self._regression_code(messages)
        if has_tools and not has_observation:
            return self._tool_call(
                "不能直接相信申报结果，先独立运行复算脚本核对系数。",
                "python_exec",
                {"code": code, "timeout_s": 10},
            )

        user_text = self._user_text(messages)
        claimed = self._claimed_coefficients(user_text)
        diag: dict[str, Any] = {}
        try:
            fit = _stats.fit(DEFAULT_DATASET_PATH, "pm25",
                             ["temp", "humidity", "wind", "so2"])
            diag = {"r2": fit["r2"], "r2_holdout": fit["r2_holdout"],
                    "n": fit["n"], "p_values": fit["p_values"],
                    "ci95": fit["ci95"], "vif": fit["vif"],
                    "data_quality": fit["data_quality"]}
        except Exception:  # pragma: no cover
            pass
        tolerance = 0.15
        recomputed = {
            "coefficients": {
                "temp": -0.8439,
                "humidity": 0.4375,
                "wind": -1.6728,
                "so2": 0.2577,
            },
            "r2": TRUTH_R2,
            "n": TRUTH_N,
        }

        if not claimed:
            return self._json(
                {
                    "summary": "上下文中未发现可独立复算的系数申报，已完成通用一致性检查，未发现异常。",
                    "recomputed": recomputed,
                    "agreement": True,
                    "metrics": diag,
                    "deviations": {},
                    "feedback": "",
                    "evidence": ["未解析到 coefficients 申报字段，跳过数值比对"],
                    "confidence": 0.7,
                }
            )

        deviations: dict[str, dict[str, float]] = {}
        worst_factor = ""
        worst_error = 0.0
        for factor, value in claimed.items():
            truth = recomputed["coefficients"][factor]
            rel_error = abs(value - truth) / abs(truth) if truth else 0.0
            deviations[factor] = {
                "claimed": round(value, 4),
                "recomputed": round(truth, 4),
                "rel_error": round(rel_error, 4),
            }
            if rel_error > worst_error:
                worst_error = rel_error
                worst_factor = factor
        agreement = all(item["rel_error"] <= tolerance for item in deviations.values())

        claimed_r2 = self._claimed_number(user_text, "r2")
        r2_note = ""
        if claimed_r2 is not None:
            r2_error = abs(claimed_r2 - TRUTH_R2) / TRUTH_R2
            if r2_error > tolerance:
                agreement = False
                r2_note = f"R² 申报 {claimed_r2:.3f}，复算 {TRUTH_R2:.4f}，偏差 {r2_error:.1%} 超过 {tolerance:.0%} 容差；"
            else:
                r2_note = f"R² 申报 {claimed_r2:.3f} 与复算 {TRUTH_R2:.4f} 一致；"

        if agreement:
            feedback = ""
            summary = "独立复算完成：申报系数与真实 OLS 结果相对误差均 ≤15%，验证通过。"
            conclusion = "复算一致，分析结论可信。"
            confidence = 0.95
            evidence = [
                f"独立 OLS 复算：wind={recomputed['coefficients']['wind']}，r2={TRUTH_R2}，n={TRUTH_N}",
                "与申报系数逐项比对，全部落在 15% 容差内",
            ]
        else:
            detail = "、".join(
                f"{factor} 申报 {item['claimed']} / 复算 {item['recomputed']}（误差 {item['rel_error']:.1%}）"
                for factor, item in deviations.items()
                if item["rel_error"] > tolerance
            )
            feedback = (
                f"独立复算发现申报系数与真实数据不一致：{detail}，最大偏差 {worst_error:.1%}"
                f"（{worst_factor}），超过 {tolerance:.0%} 容差。请使用最小二乘重新拟合"
                f"（可调用 python_exec 运行 OLS 脚本），并按真实复算值汇报全部系数与 R²。"
            )
            summary = (
                f"独立复算发现系数偏差：{worst_factor} 申报 {claimed.get(worst_factor)}、"
                f"复算 {recomputed['coefficients'][worst_factor]}（误差 {worst_error:.1%}），验证不通过。"
            )
            conclusion = "申报结果与独立复算不一致，需重新拟合后再次验证。"
            confidence = 0.95
            evidence = [
                f"独立 OLS 复算：wind={recomputed['coefficients']['wind']}，r2={TRUTH_R2}，n={TRUTH_N}",
                f"最大偏差出现在 {worst_factor}：{worst_error:.1%}",
                r2_note.strip("；") if r2_note else "R² 与复算基本一致",
            ]

        return self._json(
            {
                "summary": summary,
                "recomputed": recomputed,
                "agreement": bool(agreement),
                "metrics": diag,
                "deviations": deviations,
                "feedback": feedback,
                "conclusion": conclusion,
                "evidence": evidence,
                "confidence": confidence,
            }
        )

    def _critic(self, text: str) -> str:
        wind = self._pick_value(text, "wind", TRUTH_COEFFICIENTS["wind"])
        risks = [
            f"观测数据只能说明相关性：wind 系数 {wind:.2f} 可能受季节与城市差异混杂，不能直接解释为因果。",
            "未报告共线性诊断（VIF），温度、湿度与风速之间可能相关，需确认系数稳定。",
            "样本覆盖城市/时段有限，模型外推能力与外部效度仍需验证。",
        ]
        improvements = [
            "加入城市与季节固定效应，或改用面板模型控制混杂因素。",
            "补充稳健标准误、滞后项与非线性项做敏感性分析。",
            "增加残差诊断与留出集交叉验证，报告泛化误差。",
        ]
        return self._json(
            {
                "summary": "结论方向可信、验证闭环完整，但因果解释需谨慎，建议补充混杂控制与稳健性检验。",
                "confidence": 0.86,
                "risks": risks,
                "improvements": improvements,
                "evidence": ["评审基于申报系数、独立复算结果与验证反馈"],
            }
        )

    def _writer(self, text: str) -> str:
        temp = self._pick_value(text, "temp", TRUTH_COEFFICIENTS["temp"])
        humidity = self._pick_value(text, "humidity", TRUTH_COEFFICIENTS["humidity"])
        wind = self._pick_value(text, "wind", TRUTH_COEFFICIENTS["wind"])
        so2 = self._pick_value(text, "so2", TRUTH_COEFFICIENTS["so2"])
        r2 = self._pick_value(text, "r2", TRUTH_R2)
        n_value = self._claimed_number(text, "n")
        n = int(n_value) if n_value is not None else TRUTH_N
        title = "AI4S 空气质量与气象因子归因分析报告"
        markdown = f"""# {title}

## 一、研究问题
本报告回答 PM2.5 浓度与气象、污染物因子之间的关系：温度、湿度、风速与 SO₂ 对 PM2.5 的边际影响有多大、是否统计显著、结论是否可信。评价标准不是“模型自称正确”，而是可复现的系数、拟合优度与独立复算证据。

## 二、数据与方法
- 数据：空气质量示例数据集 `examples/datasets/air_quality.csv`，共 {n} 条观测，字段包括 date、city、pm25、temp、humidity、wind、so2、no2。
- 方法：以 pm25 为因变量，temp、humidity、wind、so2 为自变量建立多元线性回归（普通最小二乘 OLS），并由系数标准误与 t 检验给出 p 值。
- 复现：统计建模 Agent 通过 python_exec 真实运行纯标准库 OLS 脚本，验证者 Agent 另行独立复算，避免“自证”。

## 三、主要发现
| 因子 | 估计系数 | 方向解读 |
|---|---|---|
| 温度 temp | {temp:.2f} | 温度升高，PM2.5 平均下降 |
| 湿度 humidity | {humidity:.2f} | 湿度升高，PM2.5 平均上升 |
| 风速 wind | {wind:.2f} | 风速升高，PM2.5 明显下降 |
| 二氧化硫 so2 | {so2:.2f} | SO₂ 升高，PM2.5 上升 |

模型拟合优度 R² = {r2:.2f}，有效样本量 n = {n}；四个系数的 p 值均小于 0.01，说明在 5% 显著性水平下均显著。风速是影响 PM2.5 的最强负向因子，温度次之。

## 四、验证证据
- 独立复算：验证者 Agent 用同一份数据重新执行 OLS，得到 wind≈-1.6728、temp≈-0.8439、humidity≈0.4375、so2≈0.2577，与申报值相对误差均 ≤7%，agreement 为真。
- 代码复算：分析代码在子进程中真实运行，stdout 返回的系数与最终报告数值一致，可追溯、可复现。
- 统计阈值：n ≥ 1000、R² ≥ 0.55 两项门槛均满足，自循环在修复一轮后通过验证。

## 五、局限与建议
- 观测数据只能支持相关性结论，wind 与 PM2.5 的关系可能受季节、城市与污染源结构混杂，不能直接解释为因果。
- 未报告共线性诊断（VIF）与稳健标准误，建议补充面板固定效应、滞后项与非线性项做敏感性分析。
- 数据覆盖城市与时段有限，外推前应使用留出集交叉验证并报告泛化误差。

## 六、结论
在 2000 条观测样本上，风速对 PM2.5 具有最强且显著的负向效应，温度呈负向效应，湿度与 SO₂ 呈正向效应；经过独立复算与反馈修复后，申报系数与复算结果一致，结论达到数值与统计双重一致，可作为后续治理决策的量化参考。
"""
        return self._json(
            {
                "summary": "已生成中文研究报告，覆盖研究问题、数据与方法、主要发现、验证证据、局限与结论。",
                "title": title,
                "markdown": markdown,
                "evidence": [
                    "报告指标来自 statistician 的 metrics 与 verifier 的独立复算结果",
                    "markdown 长度超过 300 字，包含表格与可追溯证据",
                ],
                "confidence": 0.9,
            }
        )

    def _generic(self, text: str) -> str:
        snippet = text.strip().replace("\n", " ")[:80] or "任务"
        return self._json(
            {
                "summary": f"已完成对任务“{snippet}”的通用分析。",
                "content": {"text": "MockProvider 未识别到具体角色，返回通用结构化结果。"},
                "evidence": ["MockProvider 通用模板"],
                "confidence": 0.5,
            }
        )


__all__ = [
    "MockProvider",
    "TRUTH_COEFFICIENTS",
    "TRUTH_R2",
    "TRUTH_N",
    "FIRST_ROUND_COEFFICIENTS",
    "FIXED_COEFFICIENTS",
    "DEFAULT_DATASET_PATH",
]
