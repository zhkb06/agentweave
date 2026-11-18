#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为每个数据集生成数据卡（data_card.json）。

数据卡记录来源、生成方式、随机种子、许可、字段、质量统计与已知局限，
用于回应「数据是否可信、能否复现」的评审问题。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.llm import stats  # noqa: E402
LICENSE = "CC BY 4.0（合成数据，可自由使用与再分发）"
LIMITATIONS = [
    "数据为按已知结构函数生成的合成数据，不代表真实业务分布",
    "不含时间趋势、政策冲击与测量误差，外推到真实世界需重新标定",
    "目标与因子的函数形式假定为线性可加，非线性与交互项未建模",
    "隐藏真值仅用于验证器比对，分析 Agent 不应读取",
]


def card(path: Path, target: str, features: list[str], seed: int,
         generator: str, description: str) -> dict:
    data = stats.read_dataset(path, target, features)
    raw = path.read_bytes()
    lines = path.read_text(encoding="utf-8").splitlines()
    header = [h.strip() for h in lines[0].split(",")]
    return {
        "dataset": str(path.relative_to(ROOT)).replace("\\", "/"),
        "description": description,
        "generator": generator,
        "seed": seed,
        "license": LICENSE,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": data["n"],
        "columns": header,
        "target": target,
        "features": features,
        "quality": {
            "missing_cells": data["missing_cells"],
            "missing_rate": round(data["missing_cells"] / max(data["total_cells"], 1), 4),
            "duplicate_rows": data["duplicate_rows"],
            "outlier_rows_4sigma": data["outliers"],
            "outlier_rate": round(data["outliers"] / max(data["n"], 1), 4),
        },
        "limitations": LIMITATIONS,
        "reproduce": f"py -3.14 {generator}",
    }


def main() -> int:
    # 默认 AI4S 演示数据
    air = ROOT / "examples/datasets/air_quality.csv"
    if air.is_file():
        out = ROOT / "examples/datasets/air_quality_data_card.json"
        out.write_text(json.dumps(card(air, "pm25", ["temp", "humidity", "wind", "so2"],
                                       42, "scripts/generate_dataset.py",
                                       "AI4S 空气质量：PM2.5 与气象/污染物因子的合成观测数据"),
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print("data card:", out.relative_to(ROOT))

    # 行业场景数据
    for meta_path in sorted((ROOT / "scenarios").glob("*/scenario.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dataset = ROOT / (meta.get("dataset") or f"scenarios/{meta_path.parent.name}/data.csv")
        if not dataset.is_file():
            continue
        targets = meta.get("targets")
        target = (targets[0].get("name") if isinstance(targets[0], dict) else targets[0]) \
            if isinstance(targets, list) and targets else meta.get("target")
        result = card(dataset, str(target), list(meta.get("features") or []),
                      int(meta.get("seed") or 0),
                      "scripts/generate_scenarios.py" if not meta.get("targets") else "scripts/generate_multi_target.py",
                      meta.get("description") or meta.get("title") or meta.get("name"))
        if isinstance(targets, list) and len(targets) > 1:
            result["targets"] = [t.get("name") if isinstance(t, dict) else t for t in targets]
        out = meta_path.parent / "data_card.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("data card:", out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
