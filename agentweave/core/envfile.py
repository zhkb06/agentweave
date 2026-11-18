"""极简 ``.env`` 加载器（纯标准库、零第三方依赖）。

用途：让仓库根目录的 ``.env`` 与 ``.env.example`` / README 的承诺一致——
把 API Key 等敏感配置写进 ``.env``，重启服务即生效；``.env`` 已在
``.gitignore`` 中，绝不入库。

支持的语法（与常见 dotenv 保持一致的最小集，不做变量展开与多行值）：

- 每行 ``KEY=VALUE``，允许 ``export`` 前缀；
- ``#`` 开头的整行注释与空行忽略；
- 值可用单/双引号包裹（包裹时内容原样保留，注释写在引号外无效）；
- 未包裹时去掉行尾 `` # ...`` 注释，并 strip 两端空白；
- 空值（``KEY=``）不写入环境变量。

优先级：**真实环境变量优先**。``load_env_file`` 默认不覆盖 ``os.environ``
中已存在的同名变量，除非显式传 ``override=True``。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

__all__ = ["parse_env_text", "load_env_file"]


def _strip_inline_comment(value: str) -> str:
    """去掉未加引号值里的行尾 `` # 注释``（'#' 前无空白时按字面量保留）。"""
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in (" ", "\t"):
            return value[:index]
    return value


def _unquote(value: str) -> str:
    """去掉成对的首尾引号；不配对的引号按原文保留。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """把 ``.env`` 文本解析成 ``{键: 值}``，跳过注释、空行与非法行。"""
    result: dict[str, str] = {}
    if text.startswith("\ufeff"):  # 容忍带 BOM 的文件（utf-8-sig 之外再兜一层）
        text = text[1:]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            # 引号包裹：不做行尾注释剥离，只去成对引号
            parsed = _unquote(value)
        else:
            parsed = _strip_inline_comment(value).strip()
        if parsed:
            result[key] = parsed
    return result


def load_env_file(
    path: Path | str | None,
    environ: MutableMapping[str, str] | None = None,
    override: bool = False,
) -> list[str]:
    """读取 ``path`` 指向的 ``.env`` 并写入环境变量，返回实际写入的键名。

    - 文件不存在或读取失败时静默返回空列表（默认配置可继续工作）；
    - ``override=False``（默认）时不覆盖已存在的环境变量；
    - ``environ`` 省略时使用 ``os.environ``。
    """
    if not path:
        return []
    target = Path(path)
    env = os.environ if environ is None else environ
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        if not override and env.get(key):
            continue
        env[key] = value
        applied.append(key)
    return applied
