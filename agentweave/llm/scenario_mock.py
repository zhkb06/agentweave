#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多场景离线 Mock：让不同行业场景也能确定性演示「首轮失败 → 修复 → 通过」。

设计原则：
- **零侵入**：消息里没有 ``SCENARIO:<short>`` 标记时，完全委托原有 ``MockProvider``，
  默认「AI4S 空气质量」演示行为与测试保持不变；
- **场景可扩展**：每个 ``scenarios/<id>/scenario.json`` 自带目标列、特征、真值系数、
  首轮/修复轮偏差；本类按标记读取场景包，生成角色输出；
- **离线可复现**：统计建模师给出通用 OLS 复算代码（runtime 真跑），验证者按场景真值
  独立复算，因此 code_rerun / numeric_ground_truth / cross_agent 三层都真实生效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import stats as _stats
from .base import LLMResult
from .mock import MockProvider, estimate_tokens

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = ROOT / "scenarios"

_SCENARIO_RE = __import__("re").compile(r"SCENARIO:([a-zA-Z0-9\-_]+)")

_OLS_CODE = '''import csv, json

PATH = {path!r}
FEATURES = {features!r}
TARGET = {target!r}


def solve(matrix, vector):
    n = len(matrix)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                for j in range(col, n + 1):
                    a[r][j] -= f * a[col][j]
    return [a[i][n] for i in range(n)]


rows = []
with open(PATH, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        rows.append(row)

X = [[1.0] + [float(r[f]) for f in FEATURES] for r in rows]
y = [float(r[TARGET]) for r in rows]
k = len(X[0])
xtx = [[sum(x[i] * x[j] for x in X) for j in range(k)] for i in range(k)]
xty = [sum(x[i] * yy for x, yy in zip(X, y)) for i in range(k)]
beta = solve(xtx, xty)
pred = [sum(b * v for b, v in zip(beta, x)) for x in X]
mean_y = sum(y) / len(y)
ss_res = sum((yy - p) ** 2 for yy, p in zip(y, pred))
ss_tot = sum((yy - mean_y) ** 2 for yy in y)
r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
print(json.dumps({{"coefficients": dict(zip(["intercept"] + FEATURES, [round(b, 6) for b in beta])),
                   "r2": round(r2, 4), "n": len(rows), "p_values": {{f: 0.0 for f in FEATURES}}}},
                ensure_ascii=False))
'''


_OLS_MULTI_CODE = '''import csv, json

PATH = {path!r}
FEATURES = {features!r}
TARGETS = {targets!r}


def solve(matrix, vector):
    n = len(matrix)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                for j in range(col, n + 1):
                    a[r][j] -= f * a[col][j]
    return [a[i][n] for i in range(n)]


rows = []
with open(PATH, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        rows.append(row)

X = [[1.0] + [float(r[f]) for f in FEATURES] for r in rows]
k = len(X[0])
xtx = [[sum(x[i] * x[j] for x in X) for j in range(k)] for i in range(k)]
coefficients = {{}}
r2_values = []
for target in TARGETS:
    y = [float(r[target]) for r in rows]
    xty = [sum(x[i] * yy for x, yy in zip(X, y)) for i in range(k)]
    beta = solve(xtx, xty)
    pred = [sum(b * v for b, v in zip(beta, x)) for x in X]
    mean_y = sum(y) / len(y)
    ss_res = sum((yy - pp) ** 2 for yy, pp in zip(y, pred))
    ss_tot = sum((yy - mean_y) ** 2 for yy in y)
    r2_values.append(1.0 - ss_res / ss_tot if ss_tot else 0.0)
    for name, b in zip(FEATURES, beta[1:]):
        coefficients[target + "." + name] = round(b, 6)
print(json.dumps({{"coefficients": coefficients,
                   "r2": round(min(r2_values), 4),
                   "n": len(rows),
                   "p_values": {{key: 0.0 for key in coefficients}}}},
                ensure_ascii=False))
'''

class ScenarioMockProvider(MockProvider):
    """在 MockProvider 之上按场景包生成确定性输出。"""

    name = "mock-scenario"

    def __init__(self, base: MockProvider | None = None) -> None:
        super().__init__()
        self.base = base or MockProvider()
        self._cache: dict[str, dict[str, Any]] = {}
        self._fit_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None,
                       temperature: float = 0.2, max_tokens: int = 2048,
                       response_format: str | None = None, **kwargs: Any) -> LLMResult:
        started = time.perf_counter()
        text = self._all_text(messages)
        match = _SCENARIO_RE.search(text)
        if not match:
            return await self.base.complete(messages, tools=tools, temperature=temperature,
                                            max_tokens=max_tokens, response_format=response_format, **kwargs)

        pack = self._load(match.group(1))
        if pack is None:
            return await self.base.complete(messages, tools=tools, temperature=temperature,
                                            max_tokens=max_tokens, response_format=response_format, **kwargs)

        role = self._detect_role(messages)
        # 只认「运行时真正注入的上一轮反馈」标记，避免历史记忆里的“偏差/修复”等
        # 普通词把首轮误判成修复轮（默认 Mock 的关键字判定在通用上下文中过于宽松）。
        repair = bool(self._extract_feedback(messages).strip())
        has_observation = self._has_tool_observation(messages)
        if role == "planner":
            content = self._scenario_planner(pack)
        elif role == "researcher":
            content = self._scenario_researcher(pack)
        elif role == "data_engineer":
            content = self._scenario_data_engineer(pack)
        elif role == "statistician":
            if "JOINT_COMPARE" in text:
                content = self._scenario_joint(pack)
            else:
                content = self._scenario_statistician(pack, bool(tools), has_observation, repair)
        elif role == "verifier":
            content = self._scenario_verifier(pack, text, bool(tools), has_observation)
        elif role == "critic":
            content = self._scenario_critic(pack)
        elif role == "writer":
            content = self._scenario_writer(pack)
        else:
            content = self._json({"summary": f"{pack['name']}：执行完成。", "evidence": [], "confidence": 0.9})

        return LLMResult(
            content=content,
            tool_calls=[],
            tokens_in=sum(estimate_tokens(str(m.get("content", ""))) for m in messages),
            tokens_out=estimate_tokens(content),
            model=self.model,
            latency_s=max(time.perf_counter() - started, self.latency_s),
            raw={"mock": True, "scenario": pack["id"], "role": role, "repair": repair},
            provider=self.name,
        )

    # ------------------------------------------------------------------
    # 场景包加载
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 多目标辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _is_multi(pack: dict[str, Any]) -> bool:
        targets = pack.get("targets")
        return isinstance(targets, list) and len(targets) > 0

    @classmethod
    def _target_names(cls, pack: dict[str, Any]) -> list[str]:
        if cls._is_multi(pack):
            return [str(t.get("name") if isinstance(t, dict) else t) for t in pack["targets"]]
        return [str(pack.get("target") or "target")]

    @classmethod
    def _target_text(cls, pack: dict[str, Any]) -> str:
        if cls._is_multi(pack):
            return "、".join(cls._target_names(pack))
        return str(pack.get("target") or "目标")

    @staticmethod
    def _n(pack: dict[str, Any]) -> int:
        truth = pack.get("truth") or {}
        try:
            return int(truth.get("n") or 2000)
        except Exception:
            return 2000

    def _load(self, short: str) -> dict[str, Any] | None:
        if short in self._cache:
            return self._cache[short]
        if not SCENARIOS_DIR.is_dir():
            return None
        for meta_path in sorted(SCENARIOS_DIR.glob("*/scenario.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("short") != short and meta.get("id") != short:
                continue
            truth_path = meta_path.parent / "truth.json"
            truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.is_file() else {}
            recovery = meta.get("recovery") or truth.get("ols_recovery") or {}
            pack = dict(meta)
            pack["truth"] = truth
            pack["recovery"] = recovery
            pack["dir"] = str(meta_path.parent)
            self._cache[short] = pack
            return pack
        return None

    # ------------------------------------------------------------------
    # 角色输出
    # ------------------------------------------------------------------
    def _scenario_planner(self, pack: dict[str, Any]) -> str:
        return self._json({
            "summary": f"已把「{pack['name']}」研究目标拆成 4 个可执行任务，并给出成功判据。",
            "tasks": [
                {"id": "T1", "goal": "读取并画像数据集，确认指标口径与缺失情况", "owner": "data_engineer"},
                {"id": "T2", "goal": f"对目标 {self._target_text(pack)} 分别做多元线性回归并联合对比", "owner": "statistician"},
                {"id": "T3", "goal": "独立复算并做五层验证，识别偏差来源", "owner": "verifier"},
                {"id": "T4", "goal": "综合评审并撰写可复核报告", "owner": "writer"},
            ],
            "success_criteria": [
                "系数相对误差 ≤ 15%",
                f"R² ≥ 0.55 且样本量 ≥ 1000",
                "复算代码可独立运行并输出一致指标",
            ],
            "evidence": [f"场景包：{pack['id']}；目标列：{self._target_text(pack)}"],
            "confidence": 0.93,
        })

    def _scenario_researcher(self, pack: dict[str, Any]) -> str:
        checks = "；".join(pack.get("domain_checks") or ["检查异常值", "控制混杂"])
        return self._json({
            "summary": f"针对「{pack['name']}」选择多元线性回归 + 稳健性检查，并明确混杂控制项。",
            "methods": ["多元线性回归（OLS）", "标准化系数比较效应量", "残差与共线性检查", "分组稳健性复核"],
            "rationale": f"目标变量（{self._target_text(pack)}）为连续量，候选因子为连续/比例变量，线性可加结构可解释、可复算；{checks}。",
            "evidence": [f"数据字段：{', '.join(pack.get('features', []))}"],
            "confidence": 0.88,
        })

    def _scenario_data_engineer(self, pack: dict[str, Any]) -> str:
        return self._json({
            "summary": f"已完成 {pack['name']} 数据画像：字段类型统一、无缺失、异常值按分位裁剪。",
            "data_path": pack["dataset"],
            "rows": self._n(pack),
            "cleaning_log": [
                "所有数值列转为 float",
                "目标列与因子列缺失值检查：0 条缺失",
                "按 1%/99% 分位检查异常值，未发现需要剔除的记录",
            ],
            "evidence": ["dataset_stats 已返回列名、行数、缺失数与分位统计"],
            "confidence": 0.94,
        })

    def _fit(self, pack: dict[str, Any]) -> dict[str, Any]:
        """独立读取数据集做 OLS 诊断（缓存），避免模型自报指标。"""
        key = str(pack.get("dir") or pack.get("id"))
        if key in self._fit_cache:
            return self._fit_cache[key]
        result: dict[str, Any] = {}
        try:
            dataset = pack["dataset"]
            path = Path(dataset)
            if not path.is_absolute() and not path.exists():
                path = ROOT / dataset
            if self._is_multi(pack):
                result = _stats.fit_multi(path, self._target_names(pack), list(pack["features"]))
            else:
                result = _stats.fit(path, pack["target"], list(pack["features"]))
        except Exception:  # pragma: no cover - 数据缺失时退回场景真值
            result = {}
        self._fit_cache[key] = result
        return result

    def _scenario_statistician(self, pack: dict[str, Any], has_tools: bool,
                               has_observation: bool, repair: bool) -> str:
        features = pack["features"]
        multi = self._is_multi(pack)
        targets = self._target_names(pack)
        if multi:
            code = _OLS_MULTI_CODE.format(path=pack["dataset"], features=features, targets=targets)
        else:
            code = _OLS_CODE.format(path=pack["dataset"], features=features, target=pack["target"])
        if has_tools and not has_observation:
            return self._tool_call(
                "先用 python_exec 真实运行 OLS 复算代码，确认可复现系数后再汇报。",
                "python_exec",
                {"code": code, "timeout_s": 10},
            )

        bias = pack.get("second_bias" if repair else "first_bias") or {}
        recovery = pack.get("recovery") or {}
        if multi:
            truth_flat = pack["truth"].get("coefficients") or {}
            coefficients: dict[str, float] = {}
            for key, value in truth_flat.items():
                factor = key.split(".", 1)[1] if "." in key else key
                coefficients[key] = round(
                    float(value) * (1.0 + float(bias.get(factor, 0.0))), 4)
            fit = self._fit(pack)
            metrics = {
                "coefficients": coefficients,
                "r2": round(float(fit.get("r2") or recovery.get("r2") or 0.7), 4),
                "r2_holdout": round(float(fit.get("r2_holdout") or recovery.get("r2") or 0.7), 4),
                "n": int(fit.get("n") or self._n(pack)),
                "p_values": fit.get("p_values") or {key: 1e-12 for key in coefficients},
                "ci95": fit.get("ci95") or {},
                "vif": fit.get("vif") or {},
                "data_quality": fit.get("data_quality") or {},
                "conclusion": "；".join(f"{key}={value}" for key, value in coefficients.items()),
            }
            parts = []
            for tname in targets:
                vals = [f"{key.split('.', 1)[1]}={value}"
                        for key, value in coefficients.items() if key.startswith(tname + ".")]
                parts.append(f"{tname}: " + "、".join(vals))
            prefix = "已按验证反馈重新拟合" if repair else "已完成多目标联合回归"
            summary = f"{prefix}：{'；'.join(parts)}。R²(min)={metrics['r2']}，n={self._n(pack)}。"
            confidence = 0.9 if repair else 0.62
            evidence = ["python_exec 真实运行多目标 OLS 复算，已返回各目标系数与 R²",
                        "指标以 <目标>.<因子> 扁平键汇总，可逐项独立复核"]
        else:
            truth_c = pack["truth"].get("coefficients") or {}
            coefficients = {
                name: round(float(truth_c.get(name, 0.0)) * (1.0 + float(bias.get(name, 0.0))), 4)
                for name in features
            }
            fit = self._fit(pack)
            metrics = {
                "coefficients": coefficients,
                "r2": round(float(fit.get("r2") or recovery.get("r2") or 0.72), 4),
                "r2_holdout": round(float(fit.get("r2_holdout") or recovery.get("r2") or 0.72), 4),
                "n": int(fit.get("n") or self._n(pack)),
                "p_values": fit.get("p_values") or {name: 1e-12 for name in features},
                "ci95": fit.get("ci95") or {},
                "vif": fit.get("vif") or {},
                "data_quality": fit.get("data_quality") or {},
                "conclusion": "、".join(f"{k}={v}" for k, v in coefficients.items()),
            }
            if repair:
                summary = f"已按验证反馈重新拟合：{', '.join(f'{k}={v}' for k, v in coefficients.items())}，R²={metrics['r2']}，n={self._n(pack)}。"
                confidence = 0.9
                evidence = ["python_exec 真实运行 OLS 复算，已返回系数与 R²", "已逐项核对上一轮验证反馈中的偏差项"]
            else:
                summary = f"已完成 {pack['name']} 多元回归：{', '.join(f'{k}={v}' for k, v in coefficients.items())}，R²={metrics['r2']}，n={self._n(pack)}。"
                confidence = 0.62
                evidence = ["python_exec 真实运行 OLS 复算", "各因子 p 值均小于 0.01，统计显著"]
        return self._json({"summary": summary, "code": code, "metrics": metrics,
                           "evidence": evidence, "confidence": confidence})

    def _scenario_verifier(self, pack: dict[str, Any], text: str, has_tools: bool,
                           has_observation: bool) -> str:
        features = pack["features"]
        multi = self._is_multi(pack)
        truth = pack["truth"].get("coefficients") or {}
        recovery = pack.get("recovery") or {}
        rec_coefs = recovery.get("coefficients") or {}
        fit = self._fit(pack)
        fit_coefs = fit.get("coefficients") or {}
        diag = {k: fit.get(k) for k in ("r2", "r2_holdout", "n", "p_values", "ci95", "vif", "data_quality")} if fit else {}
        if multi:
            keys = list(truth.keys())
            recomputed = {key: round(float(fit_coefs.get(key, rec_coefs.get(key, truth.get(key, 0.0)))), 4)
                          for key in keys}
            claimed = self._claimed_coefficients(text)
            deviations: dict[str, dict[str, float]] = {}
            failures: list[str] = []
            for key in keys:
                if key not in claimed:
                    continue
                expected = float(recomputed[key])
                actual = float(claimed[key])
                rel = abs(actual - expected) / max(abs(expected), 1e-6)
                deviations[key] = {"claimed": actual, "recomputed": expected,
                                   "rel_error": round(rel, 4)}
                if rel > 0.15:
                    failures.append(f"{key}（相对误差 {rel:.1%}）")
            agreement = not failures
            if agreement:
                feedback = f"独立复算与申报一致：{len(keys)} 项多目标指标均在 15% 容差内，可进入联合对比。"
                summary = f"独立复算一致：{len(keys)} 项多目标系数全部在容差内。"
                confidence = 0.95
            else:
                feedback = ("发现偏差：" + "；".join(failures) +
                            "。请分别用最小二乘重新拟合各目标，逐项核对后给出新的系数、R² 与样本量。")
                summary = f"独立复算发现 {len(failures)} 项多目标系数超出 15% 容差，需修复后重跑。"
                confidence = 0.82
            return self._json({
                "summary": summary,
                "recomputed": {"coefficients": recomputed,
                               "r2": round(float(fit.get("r2") or recovery.get("r2") or 0.7), 4),
                               "n": int(fit.get("n") or self._n(pack))},
                "metrics": diag,
                "agreement": agreement,
                "deviations": deviations,
                "feedback": feedback,
                "evidence": [f"阅读 {pack['dataset']} 并对 {len(self._target_names(pack))} 个目标分别独立 OLS 复算",
                             feedback],
                "confidence": confidence,
            })

        recomputed = {f: round(float(fit_coefs.get(f, rec_coefs.get(f, truth.get(f, 0.0)))), 4)
                      for f in features}
        claimed = self._claimed_coefficients(text)
        deviations = {}
        failures = []
        for factor in features:
            if factor not in claimed:
                continue
            expected = float(recomputed[factor])
            actual = float(claimed[factor])
            rel = abs(actual - expected) / max(abs(expected), 1e-6)
            deviations[factor] = {"claimed": actual, "recomputed": expected, "rel_error": round(rel, 4)}
            if rel > 0.15:
                failures.append(f"{factor}（相对误差 {rel:.1%}）")
        agreement = not failures
        if agreement:
            feedback = "独立复算与申报一致，十项指标均在 15% 容差内，可进入下一阶段。"
            summary = f"独立复算一致：{len(features)} 项系数全部在容差内。"
            confidence = 0.95
        else:
            feedback = (
                "发现偏差：" + "；".join(failures) +
                "。请用最小二乘重新拟合，逐项核对后给出新的系数、R² 与样本量。"
            )
            summary = f"独立复算发现 {len(failures)} 项系数超出 15% 容差，需修复后重跑。"
            confidence = 0.82
        return self._json({
            "summary": summary,
            "recomputed": {"coefficients": recomputed,
                           "r2": round(float(fit.get("r2") or recovery.get("r2") or 0.72), 4),
                           "n": int(fit.get("n") or self._n(pack))},
            "metrics": diag,
            "agreement": agreement,
            "deviations": deviations,
            "feedback": feedback,
            "evidence": [f"阅读 {pack['dataset']} 并独立 OLS 复算", feedback],
            "confidence": confidence,
        })

    def _scenario_joint(self, pack: dict[str, Any]) -> str:
        targets = self._target_names(pack)
        features = pack["features"]
        truth_flat = pack["truth"].get("coefficients") or {}
        by_feature: dict[str, dict[str, float]] = {f: {} for f in features}
        for key, value in truth_flat.items():
            if "." not in key:
                continue
            tname, factor = key.split(".", 1)
            if factor in by_feature:
                by_feature[factor][tname] = float(value)
        common: list[str] = []
        conflicts: list[str] = []
        for factor in features:
            values = [by_feature[factor].get(t, 0.0) for t in targets]
            nonzero = [v for v in values if abs(v) > 1e-9]
            if len(nonzero) >= 2 and all(v > 0 for v in nonzero):
                common.append(f"{factor}（各目标同向为正）")
            elif len(nonzero) >= 2 and all(v < 0 for v in nonzero):
                common.append(f"{factor}（各目标同向为负）")
            elif len(nonzero) >= 2 and (max(nonzero) > 0 > min(nonzero)):
                conflicts.append(factor)
        magnitude = {f: sum(abs(by_feature[f].get(t, 0.0)) for t in targets) for f in features}
        priority = sorted(features, key=lambda f: magnitude[f], reverse=True)[:3]
        lines = []
        for tname in targets:
            pairs = [f"{f}={by_feature[f].get(tname, 0.0):+.3f}" for f in features]
            lines.append(f"- `{tname}`：" + "、".join(pairs))
        summary = (
            f"已完成 {len(targets)} 个目标的联合对比："
            f"共同抓手 {len(common)} 项、冲突因子 {len(conflicts)} 项。"
        )
        return self._json({
            "summary": summary,
            "targets": targets,
            "per_target": lines,
            "common_drivers": common,
            "conflict_factors": conflicts,
            "priority": priority,
            "joint_conclusion": (
                "优先调节共同抓手可获得多目标同时改善；冲突因子需要按业务权重做取舍，"
                "并在小范围试验中验证方向。"
            ),
            "evidence": ["基于各目标独立 OLS 系数的方向与效应量做联合比对"],
            "confidence": 0.9,
        })


    def _scenario_critic(self, pack: dict[str, Any]) -> str:
        return self._json({
            "summary": f"{pack['name']} 结论方向可信，但需说明因果边界与外推限制。",
            "risks": ["观测数据只能支持关联，不能直接下因果结论", "极端区间样本稀疏，效应估计不稳定"],
            "improvements": ["补充分组稳健性检验", "说明量纲与业务可解释性", "标注数据时间范围与地域边界"],
            "evidence": ["已核对验证中心两轮结果与偏差明细"],
            "confidence": 0.89,
        })

    def _scenario_writer(self, pack: dict[str, Any]) -> str:
        if self._is_multi(pack):
            targets = self._target_names(pack)
            flat = pack["recovery"].get("coefficients") or {}
            truth = pack["truth"].get("coefficients") or {}
            lines = [
                f"# {pack['title']}",
                "",
                f"> 场景：{pack['name']}（{pack['id']}）· 多目标联合分析（{len(targets)} 个目标）",
                "",
                "## 摘要",
                f"围绕「{pack['description']}」，本文对 {('、'.join(f'`{t}`' for t in targets))} "
                "分别建立多元线性回归模型，并做跨目标联合对比。首轮因系数偏差被验证器拦下，"
                "系统依据结构化反馈重跑，第二轮全部通过。",
                "",
                "## 数据与方法",
                f"使用 `{pack['dataset']}` 的 {self._n(pack)} 条观测，候选因子 "
                + "、".join(f"`{f}`" for f in pack["features"]) + "。各目标独立 OLS，指标以 `<目标>.<因子>` 扁平键核对。",
                "",
                "## 各目标主要发现",
            ]
            for tname in targets:
                lines += ["", f"### `{tname}`", "", "| 因子 | 估计系数 |", "|---|---|"]
                for f in pack["features"]:
                    key = f"{tname}.{f}"
                    lines.append(f"| `{f}` | {flat.get(key, truth.get(key))} |")
            lines += [
                "",
                f"R²（各目标最小值）≈ {pack['recovery'].get('r2')}，样本量 {self._n(pack)}。",
                "",
                "## 联合结论",
                "共同抓手与冲突因子已在「联合对比」节点给出：同向且效应量大的因子可作为联合优化首选；",
                "方向相反的因子需要按业务权重取舍，并建议用小范围试验验证。",
                "",
                "## 验证证据",
                "五层验证覆盖：结构、真值比对、代码重跑、跨 Agent 独立复算、统计显著性；"
                "两轮记录与逐项目标偏差均保留在验证中心，可逐项复核。",
                "",
                "## 局限与建议",
                "1. 观测数据只能支持关联性结论，因果解释需结合实验；",
                "2. 多目标之间可能存在未建模的相互影响，建议后续引入联立方程或结构模型；",
                "3. 结论仅适用于当前数据覆盖的时间与业务范围。",
            ]
            return self._json({
                "title": pack["title"],
                "markdown": "\n".join(lines),
                "summary": f"已生成《{pack['title']}》多目标联合分析报告，含 {len(targets)} 个目标的发现表与联合结论。",
                "evidence": ["分析、联合对比、验证与评审节点输出均已汇总"],
                "confidence": 0.9,
            })

        truth = pack["truth"].get("coefficients") or {}
        recovery = pack["recovery"].get("coefficients") or {}
        lines = [
            f"# {pack['title']}",
            "",
            f"> 场景：{pack['name']}（{pack['id']}）",
            "",
            "## 摘要",
            f"围绕「{pack['description']}」，本文用多元线性回归估计各因子效应，并通过五层验证复核。",
            "首轮结果因部分系数偏差超过容差被验证器拦下，系统根据结构化反馈重跑，第二轮全部通过。",
            "",
            "## 数据与方法",
            f"使用 `{pack['dataset']}` 的 {self._n(pack)} 条观测，目标列 `{pack['target']}`，候选因子 "
            + "、".join(f"`{f}`" for f in pack["features"]) + "。采用 OLS，并做残差与共线性检查。",
            "",
            "## 主要发现",
            "| 因子 | 估计系数 | 说明 |",
            "|---|---|---|",
        ]
        for f in pack["features"]:
            lines.append(f"| `{f}` | {recovery.get(f, truth.get(f))} | 控制其他因子后的边际效应 |")
        lines += [
            "",
            f"模型 R² ≈ {pack['recovery'].get('r2')}，样本量 {self._n(pack)}。系数量级与业务方向一致，可作为下一步实验设计的依据。",
            "",
            "## 验证证据",
            "五层验证：结构、真值比对、代码重跑、跨 Agent 独立复算、统计显著性。两轮记录均保留在验证中心，可逐项复核。",
            "",
            "## 局限与建议",
            "1. 观测数据只能支持关联性结论，因果解释需结合实验或领域知识；",
            "2. 极端区间样本较少，建议补充分层复核；",
            "3. 结论仅适用于当前数据覆盖的时间与地域范围。",
        ]
        return self._json({
            "title": pack["title"],
            "markdown": "\n".join(lines),
            "summary": f"已生成《{pack['title']}》研究报告，含数据与方法、主要发现、验证证据与局限。",
            "evidence": ["分析、验证与评审节点输出均已汇总"],
            "confidence": 0.9,
        })
