"""内置工具集：数据统计、文件读写、代码执行、报告渲染等（纯标准库）。

设计原则
--------
* **离线优先**：除 ``http_get`` 外全部不联网；``python_exec`` 由 AST 白名单限制
  可导入模块与危险属性，禁止网络/进程/反射类操作；
* **路径安全**：``file_read`` / ``file_write`` 只允许操作 ``workspace_dir`` 内的文件
  （``realpath`` + ``commonpath`` 双重校验，防 ``../`` 与符号链接穿越）；
* **长输出截断**：子进程 stdout/stderr 截断到 8KB，文件读取默认 200KB，避免撑爆上下文；
* **统一返回 dict**：便于直接 JSON 序列化后回注给 LLM 或写入报告。

``build_tool_registry(workspace_dir, bus=None)`` 是平台装配入口，注册全部九个内置工具。
"""
from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape as _xml_escape

from ..core.types import ToolSpec
from .registry import ToolRegistry

#: 项目根目录（agentweave/tools/builtin.py -> 上溯两级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 子进程输出截断阈值（8KB）。
MAX_OUTPUT_BYTES = 8192
#: python_exec 默认/最大超时（秒）。
DEFAULT_EXEC_TIMEOUT = 10.0
MAX_EXEC_TIMEOUT = 60.0

#: python_exec 允许导入的标准库模块（禁止网络/进程/文件系统破坏类模块）。
ALLOWED_IMPORTS = {
    "math",
    "statistics",
    "csv",
    "json",
    "random",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "re",
    "os",
    "pathlib",
    "sys",
    "time",
    "typing",
    "decimal",
    "fractions",
    "operator",
    "string",
    "textwrap",
    "heapq",
    "bisect",
    "numbers",
    "dataclasses",
    "enum",
}

#: 禁止直接调用的内建函数名。
_BLOCKED_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "input",
    "getattr",
    "setattr",
    "delattr",
    "memoryview",
}

#: 禁止访问的属性名（进程/网络/反射逃逸）。
_BLOCKED_ATTRS = {
    "system",
    "popen",
    "spawn",
    "spawnl",
    "spawnv",
    "execv",
    "execve",
    "execvp",
    "fork",
    "forkpty",
    "kill",
    "killpg",
    "modules",
    "__dict__",
    "__globals__",
    "__subclasses__",
    "__builtins__",
    "__class__",
    "__mro__",
    "__bases__",
    "__code__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
}


class PathSecurityError(PermissionError):
    """路径越界（试图访问 workspace 之外的位置）。"""


class CodeSecurityError(ValueError):
    """python_exec 代码未通过安全检查。"""


# ---------------------------------------------------------------------------
# 路径与通用工具
# ---------------------------------------------------------------------------
def _workspace(registry_or_dir: ToolRegistry | str) -> Path:
    """解析 workspace 根目录（空字符串时用当前工作目录）。"""
    if isinstance(registry_or_dir, ToolRegistry):
        raw = registry_or_dir.workspace_dir
    else:
        raw = registry_or_dir
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(
            str(root)
        )
    except ValueError:
        return False


def _resolve_in_workspace(workspace: Path, raw_path: str) -> Path:
    """把用户路径解析到 workspace 内；越界抛 :class:`PathSecurityError`。"""
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("path 不能为空")
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    if not _is_relative_to(resolved, workspace):
        raise PathSecurityError(f"路径越界：{raw} 不在 workspace 内")
    return resolved


def _resolve_readonly(workspace: Path, raw_path: str) -> Path:
    """只读工具路径解析：优先 workspace，其次项目根目录（示例数据集）。"""
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("path 不能为空")
    candidate = Path(raw)
    bases = [workspace]
    if PROJECT_ROOT != workspace:
        bases.append(PROJECT_ROOT)
    first_error: Exception | None = None
    for base in bases:
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if not _is_relative_to(resolved, base):
            first_error = first_error or PathSecurityError(f"路径越界：{raw}")
            continue
        if resolved.exists() or base is bases[-1]:
            return resolved
    if first_error is not None:
        raise first_error
    raise PathSecurityError(f"路径越界：{raw}")


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """截断长文本，返回 ``(截断后文本, 是否截断)``。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[输出已截断，共 {len(text)} 字符]", True


def _json_or_text(value: Any) -> Any:
    """尝试把字符串解析为 JSON。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


# ---------------------------------------------------------------------------
# python_exec 安全检查
# ---------------------------------------------------------------------------
def check_code_safety(code: str) -> None:
    """AST 静态检查：导入白名单 + 危险名称/属性黑名单。不通过抛异常。"""
    if not str(code or "").strip():
        raise CodeSecurityError("code 不能为空")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise CodeSecurityError(f"代码语法错误：{exc.msg} (第 {exc.lineno} 行)") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise CodeSecurityError(f"python_exec 禁止导入模块: {root}")
                if alias.name.split(".")[0] == "os" and "." in alias.name:
                    raise CodeSecurityError(f"python_exec 禁止导入子模块: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if not root or root not in ALLOWED_IMPORTS:
                raise CodeSecurityError(f"python_exec 禁止从模块导入: {module or '(相对导入)'}")
        elif isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            raise CodeSecurityError(f"python_exec 禁止使用: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _BLOCKED_ATTRS:
            raise CodeSecurityError(f"python_exec 禁止访问属性: {node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_NAMES:
                raise CodeSecurityError(f"python_exec 禁止调用: {func.id}")


def _last_json_line(stdout: str) -> Any:
    """从 stdout 取最后一个可解析为 JSON 的行（Mock 代码约定 print 一行 JSON）。"""
    text = (stdout or "").strip()
    if not text:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (ValueError, TypeError):
            continue
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _python_exec(registry: ToolRegistry, *, code: str, timeout_s: float = DEFAULT_EXEC_TIMEOUT) -> dict[str, Any]:
    """在子进程中执行受控 Python 代码（工作目录为 workspace，超时 10 秒）。"""
    check_code_safety(code)
    workspace = _workspace(registry)
    timeout = max(0.1, min(float(timeout_s or DEFAULT_EXEC_TIMEOUT), MAX_EXEC_TIMEOUT))
    tmp_dir = workspace / ".agentweave_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, script_name = tempfile.mkstemp(prefix="exec_", suffix=".py", dir=str(tmp_dir))
    script_path = Path(script_name)
    env = dict(os.environ)
    env.update(
        {
            "AGENTWEAVE_WORKSPACE": str(workspace),
            "AGENTWEAVE_ARTIFACTS": str(workspace / "artifacts"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.perf_counter()
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(code)
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
        duration = time.perf_counter() - started
        stdout, stdout_cut = _truncate(proc.stdout or "")
        stderr, stderr_cut = _truncate(proc.stderr or "")
        parsed = _last_json_line(proc.stdout or "")
        result: dict[str, Any] = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": int(proc.returncode),
            "duration_s": round(duration, 4),
            "truncated": bool(stdout_cut or stderr_cut),
            "timeout_s": timeout,
        }
        if parsed is not None:
            result["value_json"] = parsed
        return result
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"python_exec 执行超时（>{timeout:g}s）") from exc
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 各内置工具实现
# ---------------------------------------------------------------------------
def _dataset_stats(registry: ToolRegistry, *, path: str, max_rows: int = 100000) -> dict[str, Any]:
    """读取 CSV 并给出列级统计（数值列均值/标准差/最值，文本列唯一值）。"""
    workspace = _workspace(registry)
    resolved = _resolve_readonly(workspace, path)
    if not resolved.is_file():
        raise FileNotFoundError(f"数据集不存在: {path}")
    rows: list[dict[str, Any]] = []
    encoding = "utf-8-sig"
    try:
        rows = _read_csv_rows(resolved, encoding, max_rows)
    except UnicodeDecodeError:
        rows = _read_csv_rows(resolved, "gbk", max_rows)
    if not rows:
        return {
            "path": str(resolved),
            "rows": 0,
            "columns": [],
            "numeric_columns": [],
            "text_columns": [],
            "head": [],
            "missing_total": 0,
            "shape": [0, 0],
        }
    headers = list(rows[0].keys())
    columns: list[dict[str, Any]] = []
    numeric_columns: list[str] = []
    text_columns: list[str] = []
    missing_total = 0
    for header in headers:
        raw_values = [row.get(header) for row in rows]
        missing = sum(1 for v in raw_values if v is None or str(v).strip() == "")
        missing_total += missing
        values = [str(v).strip() for v in raw_values if v is not None and str(v).strip() != ""]
        numbers = _try_numbers(values)
        column: dict[str, Any] = {"name": header, "count": len(values), "missing": missing}
        if numbers is not None and values:
            column.update(
                {
                    "type": "number",
                    "mean": round(statistics.fmean(numbers), 6),
                    "std": round(statistics.pstdev(numbers), 6) if len(numbers) > 1 else 0.0,
                    "min": min(numbers),
                    "max": max(numbers),
                    "unique": len(set(numbers)),
                }
            )
            numeric_columns.append(header)
        else:
            column.update(
                {
                    "type": "text",
                    "unique": len(set(values)),
                    "top": _top_values(values, 3),
                }
            )
            text_columns.append(header)
        columns.append(column)
    head = rows[:3]
    return {
        "path": str(resolved),
        "rows": len(rows),
        "columns": columns,
        "numeric_columns": numeric_columns,
        "text_columns": text_columns,
        "head": head,
        "missing_total": missing_total,
        "shape": [len(rows), len(headers)],
    }


def _read_csv_rows(path: Path, encoding: str, max_rows: int) -> list[dict[str, Any]]:
    with path.open("r", encoding=encoding, newline="") as fh:
        reader = csv.DictReader(fh)
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(reader):
            if index >= max(1, int(max_rows or 1)):
                break
            rows.append({str(k): v for k, v in row.items() if k is not None})
        return rows


def _try_numbers(values: list[str]) -> list[float] | None:
    if not values:
        return None
    numbers: list[float] = []
    for value in values:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            return None
    return numbers


def _top_values(values: list[str], k: int) -> list[list[Any]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, int(k))]
    return [[name, count] for name, count in ordered]


def _file_read(registry: ToolRegistry, *, path: str, max_bytes: int = 200_000, encoding: str = "utf-8") -> dict[str, Any]:
    """读取 workspace 内文本文件（默认最多 200KB）。"""
    workspace = _workspace(registry)
    resolved = _resolve_in_workspace(workspace, path)
    if not resolved.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    limit = max(1, min(int(max_bytes or 200_000), 5_000_000))
    data = resolved.read_bytes()
    truncated = len(data) > limit
    text = data[:limit].decode(encoding or "utf-8", errors="replace")
    return {
        "path": str(resolved),
        "size": len(data),
        "bytes_read": min(len(data), limit),
        "truncated": truncated,
        "content": text,
    }


def _file_write(
    registry: ToolRegistry, *, path: str, content: str, append: bool = False, mkdir: bool = True
) -> dict[str, Any]:
    """写入 workspace 内文本文件（自动创建父目录）。"""
    workspace = _workspace(registry)
    resolved = _resolve_in_workspace(workspace, path)
    if mkdir:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    text = str(content if content is not None else "")
    mode = "a" if append else "w"
    with resolved.open(mode, encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return {"path": str(resolved), "bytes": len(text.encode("utf-8")), "append": bool(append)}


# -- calculator -------------------------------------------------------------
_CALC_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "pow": math.pow,
    "floor": math.floor,
    "ceil": math.ceil,
    "fabs": math.fabs,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "hypot": math.hypot,
    "degrees": math.degrees,
    "radians": math.radians,
}
_CALC_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


def _eval_ast(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("calculator 只支持数值常量")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"calculator 不支持运算符: {type(node.op).__name__}")
        left, right = _eval_ast(node.left), _eval_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(float(right)) > 1000:
            raise ValueError("calculator 指数过大")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"calculator 不支持一元运算: {type(node.op).__name__}")
        return op(_eval_ast(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CALC_CONSTS:
            return _CALC_CONSTS[node.id]
        raise ValueError(f"calculator 未知名称: {node.id}")
    if isinstance(node, ast.Call):
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "math":
            name = func.attr
        if name not in _CALC_FUNCS:
            raise ValueError(f"calculator 不支持的函数: {name or ast.dump(func)}")
        args = [_eval_ast(arg) for arg in node.args]
        return _CALC_FUNCS[name](*args)
    raise ValueError(f"calculator 不支持的表达式节点: {type(node).__name__}")


def _calculator(registry: ToolRegistry | None = None, *, expression: str) -> dict[str, Any]:
    """安全计算数学表达式（禁用 eval，AST 白名单求值）。"""
    _ = registry  # 统一签名，便于注册器绑定 registry
    expr = str(expression or "").strip()
    if not expr:
        raise ValueError("expression 不能为空")
    tree = ast.parse(expr, mode="eval")
    value = _eval_ast(tree)
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        raise ValueError("calculator 结果非有限数")
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        value = int(value)
    return {"expression": expr, "result": value}


# -- json_query -------------------------------------------------------------
_JQ_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _json_query(registry: ToolRegistry | None = None, *, data: Any, path: str = "", default: Any = None) -> dict[str, Any]:
    """按 ``a.b[0].c`` 形式查询 JSON 字段。"""
    _ = registry  # 统一签名
    current = _json_or_text(data)
    expr = str(path or "").strip()
    found = True
    if expr:
        for name, index in _JQ_TOKEN_RE.findall(expr):
            try:
                if name:
                    if not isinstance(current, dict) or name not in current:
                        found = False
                        break
                    current = current[name]
                else:
                    if not isinstance(current, (list, tuple)) or int(index) >= len(current):
                        found = False
                        break
                    current = current[int(index)]
            except (KeyError, IndexError, TypeError):
                found = False
                break
    if not found:
        return {"path": expr, "found": False, "value": default}
    return {"path": expr, "found": True, "value": current}


# -- http_get ---------------------------------------------------------------
def _http_get(
    registry: ToolRegistry | None = None, *, url: str, max_bytes: int = 200_000, timeout_s: float = 10.0
) -> dict[str, Any]:
    """GET 一个 HTTP(S) 页面（离线环境下会优雅失败）。"""
    _ = registry  # 统一签名
    target = str(url or "").strip()
    if not re.match(r"^https?://", target, re.IGNORECASE):
        raise ValueError("url 必须是 http:// 或 https:// 地址")
    limit = max(1, min(int(max_bytes or 200_000), 2_000_000))
    timeout = max(1.0, min(float(timeout_s or 10.0), 30.0))
    request = urllib.request.Request(target, headers={"User-Agent": "AgentWeave/1.0 (+offline-demo)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read(limit + 1)
            truncated = len(payload) > limit
            payload = payload[:limit]
            text = payload.decode(charset, errors="replace")
            return {
                "url": target,
                "status": status,
                "content_type": response.headers.get_content_type(),
                "bytes": len(payload),
                "truncated": truncated,
                "text": text,
            }
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"http_get 失败：HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"http_get 网络不可达（离线环境正常）：{exc.reason}") from exc


# -- chart_svg --------------------------------------------------------------
def _chart_svg(
    registry: ToolRegistry,
    *,
    title: str = "图表",
    data: Any = None,
    width: int = 640,
    height: int = 400,
    path: str = "",
) -> dict[str, Any]:
    """生成零依赖 SVG 柱状图；可选写入 workspace 文件。"""
    items: list[tuple[str, float]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            try:
                items.append((str(key), float(value)))
            except (TypeError, ValueError):
                continue
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            try:
                items.append((str(index), float(value)))
            except (TypeError, ValueError):
                continue
    if not items:
        raise ValueError("chart_svg 需要非空数值序列 data")
    w = max(240, min(int(width or 640), 2400))
    h = max(160, min(int(height or 400), 1600))
    pad_left, pad_bottom, pad_top = 48, 56, 40
    plot_w = max(1, w - pad_left - 24)
    plot_h = max(1, h - pad_bottom - pad_top)
    values = [v for _, v in items]
    vmin, vmax = min(0.0, min(values)), max(0.0, max(values))
    span = (vmax - vmin) or 1.0
    bar_w = plot_w / max(1, len(items))
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{_xml_escape(title)}">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="{w // 2}" y="24" text-anchor="middle" font-family="sans-serif" '
        f'font-size="16" fill="#111827">{_xml_escape(title)}</text>',
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top + plot_h}" stroke="#9ca3af"/>',
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{pad_left + plot_w}" '
        f'y2="{pad_top + plot_h}" stroke="#9ca3af"/>',
    ]
    for index, (label, value) in enumerate(items):
        ratio = (value - vmin) / span
        bar_h = max(1.0, ratio * plot_h)
        x = pad_left + index * bar_w + bar_w * 0.15
        y = pad_top + plot_h - bar_h
        color = "#2563eb" if value >= 0 else "#dc2626"
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w * 0.7:.2f}" height="{bar_h:.2f}" fill="{color}"/>')
        parts.append(
            f'<text x="{x + bar_w * 0.35:.2f}" y="{pad_top + plot_h + 18}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="11" fill="#374151">{_xml_escape(label)}</text>'
        )
        parts.append(
            f'<text x="{x + bar_w * 0.35:.2f}" y="{max(12.0, y - 4):.2f}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="10" fill="#4b5563">{value:g}</text>'
        )
    parts.append("</svg>")
    svg = "\n".join(parts)
    written = ""
    if path:
        workspace = _workspace(registry)
        target = _resolve_in_workspace(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg, encoding="utf-8")
        written = str(target)
    return {"svg": svg, "path": written, "width": w, "height": h, "items": len(items)}


# -- report_render ----------------------------------------------------------
def _render_section(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return [f"- {section}"]
    heading = str(section.get("heading") or section.get("title") or "").strip()
    lines: list[str] = [f"## {heading}"] if heading else []
    body = section.get("content", section.get("body", section.get("text", "")))
    items = section.get("items")
    if isinstance(items, (list, tuple)):
        lines.extend(f"- {item}" for item in items)
    if isinstance(body, str) and body.strip():
        lines.append(body.strip())
    elif isinstance(body, (list, tuple)):
        lines.extend(f"- {item}" for item in body)
    return lines


def _report_render(
    registry: ToolRegistry,
    *,
    title: str = "",
    summary: str = "",
    sections: Any = None,
    metrics: Any = None,
    conclusion: str = "",
    filename: str = "",
) -> dict[str, Any]:
    """把结构化结果渲染为 Markdown 报告，可选写入 workspace 文件。"""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    if summary:
        lines.append(f"> {summary}")
        lines.append("")
    if isinstance(sections, dict):
        sections = [{"heading": key, "content": value} for key, value in sections.items()]
    for section in sections or []:
        lines.extend(_render_section(section))
        lines.append("")
    if isinstance(metrics, dict) and metrics:
        lines.append("## 关键指标")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        for key, value in metrics.items():
            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, (list, tuple)):
                value = "、".join(str(v) for v in value)
            lines.append(f"| {key} | {value} |")
        lines.append("")
    if conclusion:
        lines.append("## 结论")
        lines.append("")
        lines.append(conclusion)
        lines.append("")
    markdown = "\n".join(lines).strip() + "\n"
    written = ""
    if filename:
        workspace = _workspace(registry)
        target = _resolve_in_workspace(workspace, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        written = str(target)
    return {"markdown": markdown, "path": written, "chars": len(markdown)}


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _builtin_specs() -> list[tuple[ToolSpec, Callable[..., Any]]]:
    """返回全部内置工具的 (规格, 实现) 列表；实现均为接收 registry 的偏函数。"""
    specs: list[tuple[ToolSpec, Callable[..., Any]]] = []

    specs.append(
        (
            ToolSpec(
                name="python_exec",
                description="在隔离子进程中执行标准库 Python 代码（禁网络），工作目录为 workspace，超时 10 秒，输出截断 8KB。",
                parameters=_schema(
                    {
                        "code": {"type": "string", "description": "要执行的 Python 源码"},
                        "timeout_s": {"type": "number", "description": "超时秒数，默认 10", "default": 10},
                    },
                    ["code"],
                ),
                risk_level="high",
                timeout_s=15.0,
                tags=["exec", "analysis"],
            ),
            _python_exec,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="calculator",
                description="安全计算数学表达式（支持 + - * / ** 与 sqrt/log/sin 等数学函数）。",
                parameters=_schema({"expression": {"type": "string"}}, ["expression"]),
                tags=["math"],
            ),
            _calculator,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="dataset_stats",
                description="读取 CSV 数据集并返回行数、列类型、均值/标准差/最值、缺失值与样例。",
                parameters=_schema(
                    {
                        "path": {"type": "string", "description": "CSV 路径（相对 workspace 或项目根目录）"},
                        "max_rows": {"type": "integer", "description": "最多统计行数", "default": 100000},
                    },
                    ["path"],
                ),
                tags=["data", "csv"],
            ),
            _dataset_stats,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="chart_svg",
                description="把数值序列渲染为零依赖 SVG 柱状图，可选写入 workspace 文件。",
                parameters=_schema(
                    {
                        "title": {"type": "string"},
                        "data": {"description": "对象 {标签: 数值} 或数值数组"},
                        "width": {"type": "integer", "default": 640},
                        "height": {"type": "integer", "default": 400},
                        "path": {"type": "string", "description": "可选输出路径"},
                    },
                    ["data"],
                ),
                tags=["chart", "artifact"],
            ),
            _chart_svg,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="file_read",
                description="读取 workspace 内的文本文件（默认最多 200KB）。",
                parameters=_schema(
                    {
                        "path": {"type": "string"},
                        "max_bytes": {"type": "integer", "default": 200000},
                        "encoding": {"type": "string", "default": "utf-8"},
                    },
                    ["path"],
                ),
                tags=["file"],
            ),
            _file_read,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="file_write",
                description="把文本写入 workspace 内文件（自动建目录，可追加）。",
                parameters=_schema(
                    {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "append": {"type": "boolean", "default": False},
                    },
                    ["path", "content"],
                ),
                risk_level="high",
                tags=["file"],
            ),
            _file_write,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="http_get",
                description="GET 一个 HTTP(S) 页面并返回文本（离线环境会返回网络不可达错误）。",
                parameters=_schema(
                    {
                        "url": {"type": "string"},
                        "max_bytes": {"type": "integer", "default": 200000},
                        "timeout_s": {"type": "number", "default": 10},
                    },
                    ["url"],
                ),
                risk_level="high",
                timeout_s=15.0,
                tags=["network"],
            ),
            _http_get,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="json_query",
                description="按 a.b[0].c 路径查询 JSON 字段，未命中时返回默认值。",
                parameters=_schema(
                    {
                        "data": {"description": "JSON 字符串或对象/数组"},
                        "path": {"type": "string", "default": ""},
                        "default": {"description": "未命中时的默认值"},
                    },
                    ["data"],
                ),
                tags=["json"],
            ),
            _json_query,
        )
    )
    specs.append(
        (
            ToolSpec(
                name="report_render",
                description="把标题、章节、指标与结论渲染为 Markdown 报告，可写入 workspace。",
                parameters=_schema(
                    {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "sections": {"type": "array", "description": "[{heading, content|items}]"},
                        "metrics": {"type": "object"},
                        "conclusion": {"type": "string"},
                        "filename": {"type": "string", "description": "可选输出路径"},
                    }
                ),
                tags=["report", "artifact"],
            ),
            _report_render,
        )
    )
    return specs


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """把九个内置工具注册到给定 registry（已注册同名工具会被覆盖）。"""
    for spec, fn in _builtin_specs():
        # 用闭包把 registry 绑定进实现，工具函数签名仍保持关键字参数形式
        registry.register(spec, _bind_registry(fn, registry))
    return registry


def _bind_registry(fn: Callable[..., Any], registry: ToolRegistry) -> Callable[..., Any]:
    def _wrapped(**kwargs: Any) -> Any:
        kwargs.pop("ctx", None)  # registry 会自动注入 ctx，内置工具不需要
        return fn(registry, **kwargs)

    _wrapped.__name__ = getattr(fn, "__name__", "tool")
    return _wrapped


def build_tool_registry(workspace_dir: str, bus: Any = None) -> ToolRegistry:
    """平台装配入口：创建带全部内置工具的 :class:`ToolRegistry`。"""
    registry = ToolRegistry(workspace_dir=str(workspace_dir or ""), bus=bus)
    register_builtin_tools(registry)
    return registry


__all__ = [
    "PROJECT_ROOT",
    "MAX_OUTPUT_BYTES",
    "ALLOWED_IMPORTS",
    "PathSecurityError",
    "CodeSecurityError",
    "check_code_safety",
    "register_builtin_tools",
    "build_tool_registry",
]
