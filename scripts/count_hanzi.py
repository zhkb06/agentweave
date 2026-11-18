# -*- coding: utf-8 -*-
"""统计 docs/方案书.md 的汉字数（全文字符中的中日韩统一表意文字）。"""
from __future__ import annotations

import re
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "docs" / "方案书.md"


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    print(f"{PATH.name}: {len(re.findall(r'[\u4e00-\u9fff]', text))} 个汉字")


if __name__ == "__main__":
    main()
