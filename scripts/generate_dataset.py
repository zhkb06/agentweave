"""生成演示数据集：AI4S 空气质量与气象因子归因分析。

数据为固定随机种子合成的日度城市观测样本，真实生成关系（隐藏真值）：

    pm25 = 78 - 0.85*temp + 0.42*humidity - 1.6*wind + 0.28*so2 + ε,
    ε ~ N(0, 8)

生成后会做一次标准库 OLS 复算，确认样本量足够把系数估计到 ±15% 以内，
并把真值写入 ``air_quality_truth.json``（只给验证器使用，不进工作流 JSON）。

用法::

    py -3.14 scripts/generate_dataset.py
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "examples" / "datasets"

SEED = 42
N_ROWS = 2000
CITIES = ["北京", "上海", "广州", "成都", "西安", "武汉"]
LEVELS = {"temp": -0.85, "humidity": 0.42, "wind": -1.6, "so2": 0.28}
INTERCEPT = 78.0
NOISE_SD = 5.0


def solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    """高斯消元解小型线性方程组（仅用于自检，纯标准库）。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("奇异矩阵")
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r != col and abs(m[r][col]) > 1e-15:
                factor = m[r][col]
                for j in range(col, n + 1):
                    m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def ols(rows: list[dict[str, float]], y_key: str = "pm25") -> dict[str, float]:
    """普通最小二乘（含截距），返回系数与 R²。"""
    xs = ["temp", "humidity", "wind", "so2"]
    k = len(xs) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    ys = [row[y_key] for row in rows]
    y_mean = sum(ys) / len(ys)
    for row in rows:
        vec = [1.0] + [row[x] for x in xs]
        for i in range(k):
            xty[i] += vec[i] * row[y_key]
            for j in range(k):
                xtx[i][j] += vec[i] * vec[j]
    beta = solve_linear(xtx, xty)
    ss_res = 0.0
    ss_tot = 0.0
    for row in rows:
        pred = beta[0] + sum(beta[i + 1] * row[x] for i, x in enumerate(xs))
        ss_res += (row[y_key] - pred) ** 2
        ss_tot += (row[y_key] - y_mean) ** 2
    out = {"intercept": round(beta[0], 4)}
    for i, x in enumerate(xs):
        out[x] = round(beta[i + 1], 4)
    out["r2"] = round(1 - ss_res / ss_tot, 4)
    return out


def main() -> None:
    rng = random.Random(SEED)
    rows: list[dict[str, float]] = []
    for i in range(N_ROWS):
        temp = 16 + 9 * math.sin(2 * math.pi * i / 365.25) + rng.gauss(0, 2.5)
        humidity = min(95.0, max(20.0, 58 + rng.gauss(0, 10)))
        wind = min(10.0, max(0.4, 3.2 + rng.gauss(0, 1.8)))
        so2 = min(30.0, max(1.0, 8 + rng.gauss(0, 3)))
        no2 = min(80.0, max(5.0, 30 - 1.2 * wind + rng.gauss(0, 6)))
        pm25 = (
            INTERCEPT
            + LEVELS["temp"] * temp
            + LEVELS["humidity"] * humidity
            + LEVELS["wind"] * wind
            + LEVELS["so2"] * so2
            + rng.gauss(0, NOISE_SD)
        )
        pm25 = max(3.0, pm25)
        day = 2021 * 10000 + ((i // 28) % 12 + 1) * 100 + (i % 28 + 1)
        rows.append(
            {
                "date": day,
                "city": CITIES[i % len(CITIES)],
                "pm25": round(pm25, 2),
                "temp": round(temp, 2),
                "humidity": round(humidity, 2),
                "wind": round(wind, 2),
                "so2": round(so2, 2),
                "no2": round(no2, 2),
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "air_quality.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["date", "city", "pm25", "temp", "humidity", "wind", "so2", "no2"]
        )
        writer.writeheader()
        writer.writerows(rows)

    fit = ols(rows)
    errors = {
        key: round(abs(fit[key] - LEVELS[key]) / abs(LEVELS[key]), 4) for key in LEVELS
    }
    truth = {
        "dataset": "air_quality.csv",
        "n": N_ROWS,
        "seed": SEED,
        "formula": "pm25 = 78 - 0.85*temp + 0.42*humidity - 1.6*wind + 0.28*so2 + ε, ε~N(0,5)",
        "intercept": INTERCEPT,
        "coefficients": dict(LEVELS),
        "noise_sd": NOISE_SD,
        "tolerance": 0.15,
        "statistical_thresholds": {"n_min": 1000, "r2_min": 0.55},
        "ols_recovery": fit,
        "recovery_rel_error": errors,
        "notes": (
            "该文件是验证器使用的隐藏真值；分析 Agent 不应读取。"
            "数据为合成数据，关系已知，用于演示'验证失败→反馈→修复→通过'闭环。"
        ),
    }
    (OUT_DIR / "air_quality_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已生成 {csv_path}（{N_ROWS} 行）")
    print("OLS 复算:", json.dumps(fit, ensure_ascii=False))
    print("系数相对误差:", json.dumps(errors, ensure_ascii=False))
    print("pm25 均值/标准差:", round(statistics.mean(r["pm25"] for r in rows), 2),
          round(statistics.pstdev([r["pm25"] for r in rows]), 2))
    worst = max(errors.values())
    print(f"最大系数误差 {worst:.2%}（要求 <15%）")
    if worst >= 0.15:
        raise SystemExit("数据不可用：系数恢复误差超标")


if __name__ == "__main__":
    main()
