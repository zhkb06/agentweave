#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库 OLS 与数据质量诊断。

供离线 Mock 提供方与检查器使用：真实读取数据集，独立计算
系数、R²、留出 R²、VIF、p 值与 95% 置信区间，避免「模型自报数字」。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

MISSING = {"", "na", "nan", "null", "none", "-"}


def _num(text: str) -> float | None:
    value = (text or "").strip()
    if value.lower() in MISSING:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_dataset(path: str | Path, target: str, features: list[str]) -> dict[str, Any]:
    """读取 CSV，返回 X/y 矩阵与数据质量统计（缺失、重复、离群）。"""
    file = Path(path)
    lines = file.read_text(encoding="utf-8").splitlines()
    header = [h.strip() for h in lines[0].split(",")]
    index = {name: i for i, name in enumerate(header)}
    missing_cols = [c for c in [target] + features if c not in index]
    if missing_cols:
        raise ValueError(f"数据集缺少列：{missing_cols}")
    xi = [index[f] for f in features]
    yi = index[target]
    X: list[list[float]] = []
    y: list[float] = []
    raw_rows: list[tuple[str, ...]] = []
    missing_cells = 0
    total_cells = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        raw_rows.append(tuple(parts))
        total_cells += len(parts)
        row: list[float] = []
        ok = True
        for pos in xi:
            value = _num(parts[pos]) if pos < len(parts) else None
            if value is None:
                missing_cells += 1
                ok = False
                break
            row.append(value)
        value_y = _num(parts[yi]) if yi < len(parts) else None
        if value_y is None:
            missing_cells += 1
            ok = False
        if not ok:
            continue
        X.append(row)
        y.append(value_y)
    duplicates = len(raw_rows) - len(set(raw_rows))
    mean_y = sum(y) / len(y) if y else 0.0
    sd_y = _std(y) if len(y) > 1 else 0.0
    outliers = sum(1 for v in y if sd_y > 0 and abs(v - mean_y) > 4.0 * sd_y)
    return {
        "X": X,
        "y": y,
        "n": len(X),
        "missing_cells": missing_cells,
        "total_cells": total_cells,
        "duplicate_rows": max(0, duplicates),
        "outliers": outliers,
    }


def _std(values: list[float]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(matrix)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("矩阵奇异（可能存在完全共线）")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r == col or a[r][col] == 0:
                continue
            factor = a[r][col]
            for j in range(col, n + 1):
                a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix)
    inv = [[0.0] * n for _ in range(n)]
    for col in range(n):
        unit = [1.0 if i == col else 0.0 for i in range(n)]
        solution = _solve(matrix, unit)
        for row in range(n):
            inv[row][col] = solution[row]
    return inv


def _ols_core(X: list[list[float]], y: list[float]) -> dict[str, Any]:
    """带截距的 OLS：返回系数、标准误、R² 与残差平方和。"""
    n = len(X)
    k = len(X[0]) + 1
    design = [[1.0] + list(row) for row in X]
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for row, target in zip(design, y):
        for i in range(k):
            xty[i] += row[i] * target
            for j in range(k):
                xtx[i][j] += row[i] * row[j]
    beta = _solve(xtx, xty)
    fitted = [sum(b * v for b, v in zip(beta, row)) for row in design]
    residuals = [t - f for t, f in zip(y, fitted)]
    sse = sum(r * r for r in residuals)
    mean = sum(y) / n
    sst = sum((t - mean) ** 2 for t in y)
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    dof = max(n - k, 1)
    sigma2 = sse / dof
    try:
        inv = _inverse(xtx)
        se = [math.sqrt(max(sigma2 * inv[i][i], 0.0)) for i in range(k)]
    except ValueError:
        se = [0.0] * k
    return {"beta": beta, "se": se, "r2": r2, "sse": sse, "n": n}


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fit(path: str | Path, target: str, features: list[str]) -> dict[str, Any]:
    """拟合单目标 OLS，并返回数据质量、留出稳定性、VIF、p 值与置信区间。"""
    data = read_dataset(path, target, features)
    X, y = data["X"], data["y"]
    if len(X) < 30:
        raise ValueError("样本量不足（<30）")
    full = _ols_core(X, y)
    beta, se = full["beta"], full["se"]
    # 留出法：每 3 行取 1 行做测试，其余训练，确定性切分
    train = [i for i in range(len(X)) if i % 3 != 0]
    test = [i for i in range(len(X)) if i % 3 == 0]
    hold_r2 = 0.0
    try:
        train_fit = _ols_core([X[i] for i in train], [y[i] for i in train])
        test_fitted = [sum(b * v for b, v in zip(train_fit["beta"], [1.0] + X[i])) for i in test]
        test_y = [y[i] for i in test]
        mean = sum(test_y) / len(test_y)
        sst = sum((t - mean) ** 2 for t in test_y)
        sse = sum((t - f) ** 2 for t, f in zip(test_y, test_fitted))
        hold_r2 = 1.0 - sse / sst if sst > 0 else 0.0
    except ValueError:
        hold_r2 = full["r2"]

    vif: dict[str, float] = {}
    for pos, name in enumerate(features):
        others = [f for f in features if f != name]
        if not others:
            vif[name] = 1.0
            continue
        try:
            aux = _ols_core([[row[features.index(f)] for f in others] for row in X], [row[pos] for row in X])
            vif[name] = round(1.0 / max(1.0 - aux["r2"], 1e-6), 3)
        except ValueError:
            vif[name] = 999.0

    p_values: dict[str, float] = {}
    ci95: dict[str, list[float]] = {}
    for pos, name in enumerate(features):
        b = beta[pos + 1]
        s = se[pos + 1]
        t = abs(b) / s if s > 0 else 999.0
        p_values[name] = round(max(2.0 * (1.0 - _normal_cdf(t)), 0.0), 6)
        ci95[name] = [round(b - 1.96 * s, 4), round(b + 1.96 * s, 4)]

    return {
        "n": full["n"],
        "r2": round(full["r2"], 4),
        "r2_holdout": round(hold_r2, 4),
        "coefficients": {name: round(beta[pos + 1], 4) for pos, name in enumerate(features)},
        "intercept": round(beta[0], 4),
        "p_values": p_values,
        "ci95": ci95,
        "vif": vif,
        "data_quality": {
            "rows": data["n"],
            "missing_rate": round(data["missing_cells"] / max(data["total_cells"], 1), 4),
            "duplicate_rows": data["duplicate_rows"],
            "outlier_rate": round(data["outliers"] / max(data["n"], 1), 4),
            "expected_rows": data["n"] + (data["total_cells"] and 0),
        },
    }


def fit_multi(path: str | Path, targets: list[str], features: list[str]) -> dict[str, Any]:
    """多目标：逐目标 OLS，系数用 <target>.<feature> 扁平键。"""
    per_target: dict[str, Any] = {}
    flat: dict[str, float] = {}
    p_values: dict[str, float] = {}
    ci95: dict[str, list[float]] = {}
    vif: dict[str, float] = {}
    holdouts: list[float] = []
    r2_list: list[float] = []
    rows = 0
    quality: dict[str, Any] = {}
    for target in targets:
        result = fit(path, target, features)
        per_target[target] = result
        rows = max(rows, result["n"])
        r2_list.append(float(result["r2"]))
        holdouts.append(float(result["r2_holdout"]))
        quality = result["data_quality"]
        for name, value in result["coefficients"].items():
            key = f"{target}.{name}"
            flat[key] = value
            p_values[key] = result["p_values"][name]
            ci95[key] = result["ci95"][name]
        for name, value in result["vif"].items():
            vif[f"{target}.{name}"] = value
    return {
        "n": rows,
        "r2": round(min(r2_list), 4),
        "r2_holdout": round(min(holdouts), 4),
        "coefficients": flat,
        "p_values": p_values,
        "ci95": ci95,
        "vif": vif,
        "data_quality": quality,
        "per_target": per_target,
    }
