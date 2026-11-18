"""安全表达式求值器。

工作流的条件分支、循环退出条件、输入变量映射都使用表达式字符串，
例如 ``quality_score >= 0.85 and iterations < 3``。

绝不使用 eval/exec：先解析为 AST，只放行白名单节点，
因此即使工作流 JSON 来自 UI 或外部仓库也不会造成代码执行。
"""
from __future__ import annotations

import ast
import operator
from typing import Any


class ExprError(ValueError):
    """表达式非法或求值失败。"""


_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_ALLOWED_FUNCS: dict[str, Any] = {
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "sum": sum,
    "sorted": sorted,
    "any": any,
    "all": all,
}

_MAX_EXPR_LEN = 2000
_MAX_POW = 10_000


def evaluate(expr: str, context: dict[str, Any] | None = None) -> Any:
    """对表达式求值并返回原始结果。"""
    if not isinstance(expr, str):
        raise ExprError(f"表达式必须是字符串，收到 {type(expr).__name__}")
    if len(expr) > _MAX_EXPR_LEN:
        raise ExprError("表达式过长")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"表达式语法错误: {expr!r} ({exc.msg})") from exc
    ctx = dict(context or {})
    ctx.setdefault("true", True)
    ctx.setdefault("false", False)
    ctx.setdefault("none", None)
    ctx.setdefault("null", None)
    return _eval(tree.body, ctx)


def truthy(expr: str, context: dict[str, Any] | None = None) -> bool:
    """条件分支使用：空表达式视为 True。"""
    if expr is None or str(expr).strip() == "":
        return True
    return bool(evaluate(expr, context))


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ctx:
            return ctx[node.id]
        if node.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[node.id]
        raise ExprError(f"未知变量: {node.id}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExprError(f"不支持的运算符: {type(node.op).__name__}")
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and right > 128:
            raise ExprError("幂运算指数过大")
        try:
            return op(left, right)
        except Exception as exc:
            raise ExprError(f"运算失败: {exc}") from exc
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ExprError(f"不支持的一元运算符: {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op_node, comp_node in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise ExprError(f"不支持的比较运算符: {type(op_node).__name__}")
            right = _eval(comp_node, ctx)
            if not op(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExprError("只允许调用白名单函数")
        fn = _ALLOWED_FUNCS.get(node.func.id)
        if fn is None:
            raise ExprError(f"未知函数: {node.func.id}")
        if node.keywords:
            raise ExprError("函数调用不支持关键字参数")
        return fn(*[_eval(a, ctx) for a in node.args])
    if isinstance(node, ast.Subscript):
        target = _eval(node.value, ctx)
        key = _eval(node.slice, ctx)
        try:
            return target[key]
        except Exception as exc:
            raise ExprError(f"下标访问失败: {exc}") from exc
    if isinstance(node, ast.List):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, ctx) for e in node.elts)
    if isinstance(node, ast.Dict):
        return {_eval(k, ctx): _eval(v, ctx) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.IfExp):
        return _eval(node.body, ctx) if _eval(node.test, ctx) else _eval(node.orelse, ctx)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append(str(_eval(value.value, ctx)))
            else:
                raise ExprError("不支持的 f-string 片段")
        return "".join(parts)
    if isinstance(node, ast.Slice):
        lower = _eval(node.lower, ctx) if node.lower else None
        upper = _eval(node.upper, ctx) if node.upper else None
        step = _eval(node.step, ctx) if node.step else None
        return slice(lower, upper, step)
    raise ExprError(f"不支持的表达式语法: {type(node).__name__}")


def render_template(template: str, context: dict[str, Any], keep_missing: bool = False) -> str:
    """渲染 ``{{var}}`` 模板；缺失变量默认替换为空串。

    使用简单的字符串扫描而非正则，避免嵌套花括号歧义。
    """
    if not template:
        return ""
    out: list[str] = []
    i = 0
    while i < len(template):
        start = template.find("{{", i)
        if start < 0:
            out.append(template[i:])
            break
        out.append(template[i:start])
        end = template.find("}}", start + 2)
        if end < 0:
            out.append(template[start:])
            break
        name = template[start + 2 : end].strip()
        value = _resolve_path(name, context)
        if value is None and not keep_missing:
            out.append("")
        else:
            out.append("" if value is None else str(value))
        i = end + 2
    return "".join(out)


def _resolve_path(path: str, context: dict[str, Any]) -> Any:
    """支持 ``result.metrics.r2`` 这样的点路径访问。"""
    cur: Any = context
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            cur = getattr(cur, part, None)
            if cur is None:
                return None
    return cur
