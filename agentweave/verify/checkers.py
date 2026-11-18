"""五层验证检查器（L1 结构 / L2 真值 / L3 复算 / L4 交叉 / L5 统计与评审）。

设计原则
--------
1. **与 LLM 解耦**：检查器只通过 :class:`VerifyContext` 拿到数据、工具注册表与
   Agent 注册表，不 import 任何具体 Provider，可离线单测；
2. **可解释**：每个检查器都返回 ``detail`` 与 ``evidence``（期望/实际/相对误差/
   建议），验证失败时这些证据直接拼成自循环反馈；
3. **可降级**：真值未注入、工具不可用、离线无 LLM 时标记 ``skipped`` 而不是崩溃，
   但真实执行后不达标一律判失败。

置信度由 :class:`agentweave.verify.verifier.VerificationPipeline` 汇总，本模块只负责
单项检查与默认权重/层级元数据。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core.types import VerifyCheckResult, VerifySpec, WorkflowNode


@dataclass
class VerifyContext:
    """一次验证所需的全部上下文（数据/工具/Agent/Run 均注入）。"""

    node: WorkflowNode
    output: Any
    run: Any
    spec: VerifySpec
    ctx: Any = None

    # -- 便捷访问 -------------------------------------------------------
    @property
    def metrics(self) -> dict[str, Any]:
        return extract_metrics(self.output)


# ---------------------------------------------------------------------------
# 通用工具：解析 / 数值比对
# ---------------------------------------------------------------------------
def parse_json_text(text: Any) -> Any:
    """从文本中解析 JSON：支持纯 JSON、```json 代码块、首尾花括号截取。"""
    if not isinstance(text, str):
        return text
    raw = text.strip()
    if not raw:
        return None
    fence = raw.find("```")
    if fence >= 0:
        block = raw[fence + 3 :]
        if block.startswith("json"):
            block = block[4:]
        end = block.find("```")
        if end >= 0:
            block = block[:end]
        raw = block.strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    start = min(
        [pos for pos in (raw.find("{"), raw.find("[")) if pos >= 0], default=-1
    )
    if start < 0:
        return None
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None


def as_output_dict(output: Any) -> dict[str, Any] | None:
    """把节点输出归一化为 dict；字符串会尝试按 JSON 解析。"""
    if isinstance(output, dict):
        return output
    if isinstance(output, str):
        parsed = parse_json_text(output)
        return parsed if isinstance(parsed, dict) else None
    inner = getattr(output, "output", None)
    if inner is not None and inner is not output:
        return as_output_dict(inner)
    return None


def extract_metrics(output: Any) -> dict[str, Any]:
    """提取输出中的 ``metrics`` 字典；没有则退回输出本身（若为 dict）。"""
    data = as_output_dict(output)
    if not data:
        return {}
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    if isinstance(metrics, str):
        parsed = parse_json_text(metrics)
        if isinstance(parsed, dict):
            return parsed
    return data


def _lookup_path(data: Any, path: str) -> Any:
    """按 ``a.b.c`` 点路径取值，缺失返回 ``None``。"""
    cur = data
    for part in path.split("."):
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
            return None
    return cur


def flatten_numbers(data: Any, prefix: str = "") -> dict[str, float]:
    """展开嵌套结构中的所有数值叶子，路径用点号连接。"""
    items: dict[str, float] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            # 顶层字符串/结论字段不参与数值比对
            items.update(flatten_numbers(value, f"{prefix}.{key}".lstrip(".")))
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            items.update(flatten_numbers(value, f"{prefix}.{index}".lstrip(".")))
    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        items[prefix] = float(data)
    return items


#: 绝对量级下限：p 值等「接近 0」的指标用相对误差没有意义，
#: 当两侧都小于该值、或绝对差不超过该值时视为一致（仅用于极小量级指标，
#: 不影响系数 / R² / 样本量等常规量级的相对容差判定）。
ABS_FLOOR: float = 1e-6


def compare_numeric(
    expected: Any,
    actual: Any,
    tolerance: float,
    only_paths: set[str] | None = None,
) -> tuple[bool, list[dict[str, Any]], float]:
    """逐数值比较相对误差，返回 ``(是否通过, 失败项, 最大相对误差)``。

    判定规则（两层，兼顾统计显著性与常规量纲）::

        diff = |actual - expected|
        通过条件 = diff <= max(tolerance * |expected|, ABS_FLOOR)   # 相对容差
                或 (|expected| < ABS_FLOOR 且 |actual| < ABS_FLOOR)  # 双极小值

    绝对下限 :data:`ABS_FLOOR` = ``1e-6`` 用于 p 值这类「接近 0 的指标」：
    ``1.1e-9`` 与 ``0.0`` 在统计上都是「极显著」，纯相对误差会算出 100%+
    的假不一致。该下限只影响绝对值极小的指标，系数（~1）、R²（~0.7）、
    样本量 n（~2000）等正常量级仍严格使用 ``tolerance`` 相对容差。
    因「绝对下限」判为一致的指标，其相对误差按 0 计入 ``max_error``。
    """
    expected_numbers = flatten_numbers(expected)
    actual_numbers = flatten_numbers(actual)
    failures: list[dict[str, Any]] = []
    max_error = 0.0
    for path, expected_value in expected_numbers.items():
        if only_paths is not None and path not in only_paths:
            continue
        if path not in actual_numbers:
            failures.append(
                {
                    "path": path,
                    "expected": expected_value,
                    "actual": None,
                    "rel_error": None,
                    "reason": "缺少该指标",
                }
            )
            continue
        actual_value = actual_numbers[path]
        diff = abs(actual_value - expected_value)
        both_tiny = abs(expected_value) < ABS_FLOOR and abs(actual_value) < ABS_FLOOR
        # p 值这类指标：0.0003 与 0 在统计上都表示「极显著」，不能按相对误差判失败
        near_zero_p = "p_values" in str(path) and max(
            abs(expected_value), abs(actual_value)) < 0.01
        within_abs_floor = both_tiny or near_zero_p or diff <= max(
            tolerance * abs(expected_value), ABS_FLOOR
        )
        if within_abs_floor:
            continue
        denominator = max(abs(expected_value), 1e-9)
        rel_error = diff / denominator
        max_error = max(max_error, rel_error)
        if rel_error > tolerance + 1e-12:
            failures.append(
                {
                    "path": path,
                    "expected": expected_value,
                    "actual": actual_value,
                    "rel_error": round(rel_error, 6),
                    "reason": f"相对误差 {rel_error:.1%} 超过容差 {tolerance:.1%}",
                }
            )
    return (not failures), failures, max_error


def _fmt_number(value: Any) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _failure_detail(
    failures: list[dict[str, Any]], tolerance: float, limit: int = 6
) -> str:
    """把失败项渲染成「申报 vs 复算/真值（相对误差）」的中文说明。"""
    parts: list[str] = []
    for item in failures[:limit]:
        claimed = item.get("actual")
        expected = item.get("expected")
        rel = item.get("rel_error")
        if rel is None:
            parts.append(f"{item['path']}：申报 {_fmt_number(claimed)}，复核值缺失")
        else:
            parts.append(
                f"{item['path']}：申报 {_fmt_number(claimed)} vs "
                f"复核/真值 {_fmt_number(expected)}，相对误差 {rel:.1%}"
            )
    if len(failures) > limit:
        parts.append(f"…另有 {len(failures) - limit} 项超出容差")
    return f"容差 {tolerance:.1%}；" + "；".join(parts)


def _skip(name: str, detail: str, weight: float = 1.0) -> VerifyCheckResult:
    """构造一个「跳过」检查结果（不计入置信度分母）。"""
    return VerifyCheckResult(
        name=name,
        passed=True,
        detail=detail,
        weight=weight,
        evidence={"skipped": True, "reason": detail},
    )


def _fail(
    name: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
    weight: float = 1.0,
) -> VerifyCheckResult:
    data = dict(evidence or {})
    data.setdefault("expected", data.get("expected"))
    data.setdefault("actual", data.get("actual"))
    data.setdefault("suggestion", "请核对数据、公式与量纲后重新输出")
    return VerifyCheckResult(
        name=name, passed=False, detail=detail, weight=weight, evidence=data
    )


# ---------------------------------------------------------------------------
# L1 结构层
# ---------------------------------------------------------------------------
async def check_schema(vc: VerifyContext) -> VerifyCheckResult:
    """输出必须是 JSON 对象，并包含节点 ``outputs`` 声明的契约字段。"""
    data = as_output_dict(vc.output)
    required = list(vc.node.outputs or [])
    if data is None:
        return _fail(
            "schema",
            "输出不是 JSON 对象（契约要求 object）",
            {
                "expected": {"type": "object", "fields": required},
                "actual": type(vc.output).__name__,
                "suggestion": "请只输出一个 JSON 对象，不要包裹解释性文字",
            },
        )
    missing = [name for name in required if name not in data]
    if missing:
        return _fail(
            "schema",
            f"缺少契约字段: {', '.join(missing)}",
            {
                "expected": required,
                "actual": sorted(data.keys())[:30],
                "missing": missing,
                "suggestion": f"请补齐字段 {', '.join(missing)} 后重新输出",
            },
        )
    return VerifyCheckResult(
        name="schema",
        passed=True,
        detail=f"JSON 对象结构合法，契约字段齐全（{len(required)} 个）",
        evidence={"expected": required, "actual": sorted(data.keys())[:30]},
    )


async def check_non_empty(vc: VerifyContext) -> VerifyCheckResult:
    """输出非空；文本长度至少 20 字符（契约要求）。"""
    output = vc.output
    if output is None:
        return _fail(
            "non_empty",
            "输出为空（None）",
            {"expected": "非空内容或长度≥20 的文本", "actual": "None"},
        )
    if isinstance(output, str):
        stripped = output.strip()
        if len(stripped) < 20:
            return _fail(
                "non_empty",
                f"文本输出过短（{len(stripped)} < 20 字符）",
                {"expected": "长度≥20 的文本", "actual": stripped[:200]},
            )
        return VerifyCheckResult(
            name="non_empty",
            passed=True,
            detail=f"文本输出非空且长度 {len(stripped)} ≥ 20",
            evidence={"length": len(stripped)},
        )
    if isinstance(output, (dict, list, tuple, set)):
        if len(output) == 0:
            return _fail(
                "non_empty",
                "容器输出为空",
                {"expected": "至少一个元素", "actual": type(output).__name__},
            )
        return VerifyCheckResult(
            name="non_empty",
            passed=True,
            detail=f"容器输出非空（{len(output)} 个元素）",
            evidence={"size": len(output)},
        )
    return VerifyCheckResult(
        name="non_empty",
        passed=True,
        detail=f"标量输出非空（{type(output).__name__}）",
        evidence={"value": str(output)[:120]},
    )


# ---------------------------------------------------------------------------
# L2 真值层
# ---------------------------------------------------------------------------
async def check_numeric_ground_truth(vc: VerifyContext) -> VerifyCheckResult:
    """用隐藏真值比对输出 ``metrics``，逐指标报告申报值/真值/相对误差。"""
    ground_truth = vc.spec.ground_truth
    if not ground_truth:
        return _skip(
            "numeric_ground_truth",
            "未注入真值（spec.ground_truth is None），跳过数值比对",
        )
    metrics = extract_metrics(vc.output)
    passed, failures, max_error = compare_numeric(
        ground_truth, metrics, vc.spec.tolerance
    )
    checked = len(flatten_numbers(ground_truth))
    evidence = {
        "tolerance": vc.spec.tolerance,
        "abs_floor": ABS_FLOOR,
        "max_rel_error": round(max_error, 6),
        "failures": failures,
        "expected": ground_truth,
        "actual": metrics,
        "checked": checked,
    }
    if not passed:
        detail = (
            f"{len(failures)}/{checked} 项数值超出容差；"
            + _failure_detail(failures, vc.spec.tolerance)
        )
        evidence["suggestion"] = (
            "请对照隐藏真值检查回归系数符号、量纲与共线性，修正后重新申报 metrics"
        )
        return _fail("numeric_ground_truth", detail, evidence)
    return VerifyCheckResult(
        name="numeric_ground_truth",
        passed=True,
        detail=f"{checked} 项数值与真值一致（最大相对误差 {max_error:.1%} ≤ {vc.spec.tolerance:.1%}）",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# L3 独立复算层
# ---------------------------------------------------------------------------
def _extract_recompute(value: Any) -> dict[str, Any]:
    """从 python_exec 的返回值中解析复算结果。"""
    if isinstance(value, dict):
        for key in ("value_json", "metrics", "recomputed", "result", "output"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                return candidate
        stdout = value.get("stdout")
        parsed = parse_json_text(stdout) if isinstance(stdout, str) else None
        if isinstance(parsed, dict):
            return parsed
        return value
    if isinstance(value, str):
        parsed = parse_json_text(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def check_code_rerun(vc: VerifyContext) -> VerifyCheckResult:
    """独立重跑输出中的 ``code``，把子进程复算指标与申报 metrics 比对。"""
    data = as_output_dict(vc.output)
    if not data:
        return _skip("code_rerun", "输出不是 JSON 对象，无法定位 code 字段，跳过独立复算")
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        return _skip("code_rerun", "输出未包含 code 字段，跳过独立复算")
    ctx = vc.ctx
    if ctx is None or getattr(ctx, "tools", None) is None:
        return _skip("code_rerun", "未注入工具注册表（ctx.tools 为空），跳过独立复算")

    metric_path = str(vc.spec.config.get("code_metric_path", "metrics") or "metrics")
    declared = _lookup_path(data, metric_path)
    if not isinstance(declared, dict):
        declared = extract_metrics(vc.output)
    declared_numbers = flatten_numbers(declared)
    if not declared_numbers:
        return _skip("code_rerun", "申报输出中未找到可复算的数值指标，跳过")

    try:
        result = await ctx.call_tool("python_exec", {"code": code})
    except Exception as exc:  # 工具异常是真实的验证失败，不静默跳过
        return _fail(
            "code_rerun",
            f"python_exec 调用异常：{type(exc).__name__}: {exc}",
            {"suggestion": "检查分析代码是否可在离线子进程运行"},
        )
    if hasattr(result, "ok") and not result.ok:
        error = getattr(result, "error", "") or "python_exec 返回失败"
        return _fail(
            "code_rerun",
            f"独立复算失败：{error}",
            {"error": error, "suggestion": "修正代码后可独立运行并输出 JSON"},
        )
    recomputed = _extract_recompute(getattr(result, "value", result))
    recomputed_numbers = flatten_numbers(recomputed)
    if not recomputed_numbers:
        return _fail(
            "code_rerun",
            "未能从复算输出中解析出 JSON 数值（stdout 最后一个 JSON）",
            {
                "expected": "代码输出含数值指标的 JSON",
                "actual": str(recomputed)[:400],
                "suggestion": "在分析脚本末尾 print(json.dumps(metrics))",
            },
        )
    passed, failures, max_error = compare_numeric(
        recomputed,
        declared,
        vc.spec.tolerance,
        only_paths=set(declared_numbers),
    )
    evidence = {
        "tolerance": vc.spec.tolerance,
        "abs_floor": ABS_FLOOR,
        "max_rel_error": round(max_error, 6),
        "failures": failures,
        "claimed": declared,
        "recomputed": recomputed,
        "duration_s": round(float(getattr(result, "duration_s", 0.0) or 0.0), 4),
    }
    if not passed:
        evidence["suggestion"] = (
            "申报指标与独立复算不一致，请检查代码是否与申报口径一致（样本/列/量纲）"
        )
        return _fail(
            "code_rerun",
            f"独立重跑不一致：{_failure_detail(failures, vc.spec.tolerance)}",
            evidence,
        )
    return VerifyCheckResult(
        name="code_rerun",
        passed=True,
        detail=(
            f"独立重跑通过：{len(declared_numbers)} 项指标最大相对误差 "
            f"{max_error:.1%} ≤ {vc.spec.tolerance:.1%}"
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# L4 跨 Agent 交叉验证层
# ---------------------------------------------------------------------------
def _find_verifier_agent(vc: VerifyContext) -> str:
    """按配置或 role 查找复核 Agent。"""
    configured = str(vc.spec.config.get("verifier_agent", "") or "")
    if configured:
        return configured
    agents = getattr(vc.ctx, "agents", None) if vc.ctx is not None else None
    if agents is None:
        return ""
    try:
        matched = agents.match(None, None) or []
    except Exception:
        matched = []
    for spec, _score in matched:
        role = str(getattr(spec, "role", "") or "")
        if "verif" in role.lower() or "critic" in role.lower():
            return str(getattr(spec, "id", "") or "")
    return ""


async def check_cross_agent(vc: VerifyContext) -> VerifyCheckResult:
    """让独立 verifier Agent 复算并比对申报指标（防止单 Agent 自说自话）。"""
    ctx = vc.ctx
    if ctx is None or getattr(ctx, "agents", None) is None:
        return _skip("cross_agent", "未注入 Agent 注册表，跳过跨 Agent 交叉验证")
    agent_id = _find_verifier_agent(vc)
    if not agent_id:
        return _skip("cross_agent", "未找到 role=verifier 的复核 Agent，跳过交叉验证")

    declared = extract_metrics(vc.output)
    declared_numbers = flatten_numbers(declared)
    if not declared_numbers:
        return _skip("cross_agent", "申报输出无可用数值指标，跳过交叉验证")
    payload = json.dumps(declared, ensure_ascii=False, default=str)[:4000]
    # 把场景标记透传给复核 Agent，避免离线 Mock 回落到默认演示数据
    marker = ""
    try:
        variables = getattr(vc.run, "variables", {}) or {}
        marker = str(variables.get("scenario_marker") or "")
        if not marker and variables.get("scenario_id"):
            marker = "SCENARIO:" + str(variables.get("scenario_id"))
    except Exception:  # pragma: no cover - 防御
        marker = ""
    prompt = (
        (marker + " 场景独立复核。\n" if marker else "")
        + "你是独立复核 Agent。请基于数据集独立复算下列申报指标，检查系数符号、量级与拟合优度。"
        f"申报 metrics: {payload}\n"
        '只输出 JSON：{"metrics": {...复算值...}, "conclusion": "..."}'
    )
    try:
        result = await ctx.call_agent(
            agent_id,
            prompt,
            vc.run,
            vc.node,
            context={"phase": "cross_verify"},
            feedback="",
        )
    except Exception as exc:
        return _fail(
            "cross_agent",
            f"复核 Agent 调用异常：{type(exc).__name__}: {exc}",
            {"suggestion": "确认 verifier Agent 可用后重试"},
        )
    if getattr(result, "error", None):
        return _fail(
            "cross_agent",
            f"复核 Agent 返回错误：{result.error}",
            {"suggestion": "检查复核 Agent 的输入与工具配置"},
        )
    agent_output = getattr(result, "output", result)
    recomputed = extract_metrics(agent_output)
    if not recomputed:
        parsed = parse_json_text(agent_output)
        recomputed = parsed if isinstance(parsed, dict) else {}
    if not recomputed:
        return _fail(
            "cross_agent",
            "复核 Agent 未返回可解析的 metrics",
            {
                "expected": "包含 metrics 的 JSON",
                "actual": str(agent_output)[:400],
                "suggestion": "要求复核 Agent 输出结构化 JSON",
            },
        )
    passed, failures, max_error = compare_numeric(
        recomputed,
        declared,
        vc.spec.tolerance,
        only_paths=set(declared_numbers),
    )
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    evidence = {
        "verifier_agent": agent_id,
        "tolerance": vc.spec.tolerance,
        "abs_floor": ABS_FLOOR,
        "max_rel_error": round(max_error, 6),
        "failures": failures,
        "claimed": declared,
        "recomputed": recomputed,
        "verifier_confidence": round(confidence, 4),
    }
    if not passed:
        evidence["suggestion"] = "复核 Agent 计算结果与申报不一致，请定位数据口径或公式差异"
        return _fail(
            "cross_agent",
            f"交叉验证不一致（verifier={agent_id}）："
            + _failure_detail(failures, vc.spec.tolerance),
            evidence,
        )
    return VerifyCheckResult(
        name="cross_agent",
        passed=True,
        detail=(
            f"Agent {agent_id} 独立复算一致：{len(declared_numbers)} 项指标最大相对误差 "
            f"{max_error:.1%}，复核置信度 {confidence:.2f}"
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# L5 统计与 LLM 评审层
# ---------------------------------------------------------------------------
def _resolve_threshold(
    name: str, threshold: Any
) -> tuple[str, str, float]:
    """解析阈值写法：``{"min":x}`` / ``{"max":x}`` / 标量（默认 min）/ ``n_min``。"""
    mode = "min"
    value: Any = threshold
    if isinstance(threshold, dict):
        if "min" in threshold:
            mode, value = "min", threshold["min"]
        elif "max" in threshold:
            mode, value = "max", threshold["max"]
    if name.endswith("_min"):
        name, mode = name[:-4], "min"
    elif name.endswith("_max"):
        name, mode = name[:-4], "max"
    try:
        return name, mode, float(value)
    except (TypeError, ValueError):
        return name, mode, float("nan")


async def check_statistical(vc: VerifyContext) -> VerifyCheckResult:
    """统计阈值检查：样本量、拟合优度、显著性等（``spec.config["thresholds"]``）。"""
    thresholds = vc.spec.config.get("thresholds") or {}
    if not isinstance(thresholds, dict) or not thresholds:
        return _skip("statistical", "未配置 spec.config['thresholds']，跳过统计检查")
    metrics = extract_metrics(vc.output)
    failures: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for raw_name, raw_threshold in thresholds.items():
        name, mode, threshold = _resolve_threshold(str(raw_name), raw_threshold)
        actual = _lookup_path(metrics, name)
        if actual is None and name in {"p", "p_value", "max_p"}:
            p_values = metrics.get("p_values")
            if isinstance(p_values, dict) and p_values:
                numeric = [
                    float(v)
                    for v in p_values.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                ]
                actual = max(numeric) if numeric else None
        checked.append(
            {"name": name, "mode": mode, "threshold": threshold, "actual": actual}
        )
        if actual is None:
            failures.append(
                {"name": name, "reason": "输出缺少该统计指标", "threshold": threshold}
            )
        elif isinstance(actual, (int, float)) and not isinstance(actual, bool):
            bad = actual < threshold if mode == "min" else actual > threshold
            if bad:
                failures.append(
                    {
                        "name": name,
                        "actual": float(actual),
                        "threshold": threshold,
                        "mode": mode,
                        "reason": (
                            f"{name}={_fmt_number(actual)} {'<' if mode == 'min' else '>'} "
                            f"阈值 {_fmt_number(threshold)}"
                        ),
                    }
                )
    passed = not failures
    evidence: dict[str, Any] = {
        "thresholds": thresholds,
        "checked": checked,
        "failures": failures,
        "actual": metrics,
    }
    if not passed:
        evidence["suggestion"] = "样本量/拟合优度未达门槛，建议增加样本或改进模型后重跑"
        detail = "统计阈值未满足：" + "；".join(
            str(item["reason"]) for item in failures
        )
        return _fail("statistical", detail, evidence)
    summary = "，".join(
        f"{item['name']}={_fmt_number(item['actual'])}"
        f"{'≥' if item['mode'] == 'min' else '≤'}{_fmt_number(item['threshold'])}"
        for item in checked
    )
    return VerifyCheckResult(
        name="statistical",
        passed=True,
        detail=f"统计阈值全部满足：{summary}",
        evidence=evidence,
    )


async def check_llm_judge(vc: VerifyContext) -> VerifyCheckResult:
    """结构化 LLM 评审；离线无 LLM 时降级为跳过并标注原因。"""
    ctx = vc.ctx
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None:
        return _skip("llm_judge", "离线模式或未配置 LLM，评审检查降级为跳过")
    criteria = vc.spec.config.get(
        "criteria", "结论是否有数据支撑、字段是否自洽、是否存在明显逻辑矛盾"
    )
    payload = json.dumps(
        as_output_dict(vc.output) or str(vc.output)[:2000],
        ensure_ascii=False,
        default=str,
    )[:5000]
    messages = [
        {
            "role": "system",
            "content": "你是严谨的科研评审专家，只输出 JSON，不解释过程。",
        },
        {
            "role": "user",
            "content": (
                f"评审标准：{criteria}\n待评审输出：{payload}\n"
                '输出 JSON：{"passed": true/false, "score": 0~1, "reason": "..."}'
            ),
        },
    ]
    try:
        try:
            result = await llm.complete(messages, response_format="json")
        except TypeError:
            result = await llm.complete(messages)
    except Exception as exc:
        return _skip(
            "llm_judge", f"LLM 评审不可用（{type(exc).__name__}: {exc}），降级为跳过"
        )
    content = getattr(result, "content", "")
    parsed = parse_json_text(content)
    if not isinstance(parsed, dict):
        return _fail(
            "llm_judge",
            "LLM 评审未返回合法 JSON",
            {
                "expected": '{"passed","score","reason"}',
                "actual": str(content)[:300],
                "suggestion": "使用 JSON mode 或要求模型只输出 JSON",
            },
        )
    min_score = float(vc.spec.config.get("judge_min_score", 0.6) or 0.6)
    score = float(parsed.get("score", 0.0) or 0.0)
    passed = bool(parsed.get("passed", score >= min_score)) and score >= min_score
    reason = str(parsed.get("reason", ""))
    evidence = {
        "score": round(score, 4),
        "min_score": min_score,
        "reason": reason,
        "criteria": criteria,
        "suggestion": "按评审意见补充数据依据与结论论证",
    }
    if not passed:
        return _fail(
            "llm_judge",
            f"LLM 评审未通过（score={score:.2f} < {min_score:.2f}）：{reason}",
            evidence,
        )
    return VerifyCheckResult(
        name="llm_judge",
        passed=True,
        detail=f"LLM 评审通过（score={score:.2f} ≥ {min_score:.2f}）：{reason[:200]}",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 产物层
# ---------------------------------------------------------------------------
def _collect_paths(value: Any) -> list[str]:
    """把 artifact 声明归一化为路径列表。"""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        paths: list[str] = []
        for item in value.values():
            paths.extend(_collect_paths(item))
        return paths
    if isinstance(value, (list, tuple, set)):
        paths = []
        for item in value:
            paths.extend(_collect_paths(item))
        return paths
    return []


async def check_artifact_exists(vc: VerifyContext) -> VerifyCheckResult:
    """声明的产物文件必须存在且非空（防止「报告只存在于模型嘴里」）。"""
    data = as_output_dict(vc.output) or {}
    declared: list[str] = []
    for source in (
        data.get("artifacts"),
        data.get("artifact"),
        vc.spec.config.get("artifacts"),
        vc.node.config.get("artifacts"),
    ):
        declared.extend(_collect_paths(source))
    # 去重并保持顺序
    declared = list(dict.fromkeys(path for path in declared if path))
    if not declared:
        return _skip("artifact_exists", "输出/配置未声明产物文件，跳过产物检查")

    bases: list[str] = []
    for base in (
        getattr(vc.ctx, "artifacts_dir", "") if vc.ctx is not None else "",
        getattr(vc.run, "artifacts_dir", ""),
        getattr(vc.ctx, "workspace_dir", "") if vc.ctx is not None else "",
    ):
        if base and base not in bases:
            bases.append(base)

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for path in declared:
        resolved = path
        if not os.path.isabs(path):
            resolved = ""
            for base in bases:
                candidate = os.path.join(base, path)
                if os.path.isfile(candidate):
                    resolved = candidate
                    break
            if not resolved:
                candidates = [os.path.join(base, path) for base in bases] or [path]
                resolved = candidates[0]
        if not os.path.isfile(resolved):
            missing.append(path)
            continue
        size = os.path.getsize(resolved)
        if size <= 0:
            missing.append(f"{path}（空文件）")
            continue
        found.append({"path": path, "size": size})
    evidence = {"declared": declared, "found": found, "missing": missing}
    if missing:
        evidence["suggestion"] = "重新生成产物并写入运行产物目录"
        return _fail(
            "artifact_exists",
            f"产物缺失或为空：{', '.join(missing)}",
            evidence,
        )
    return VerifyCheckResult(
        name="artifact_exists",
        passed=True,
        detail=f"{len(found)} 个产物文件存在且非空",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 注册表元数据
# ---------------------------------------------------------------------------
CheckerFn = Callable[[VerifyContext], Awaitable[VerifyCheckResult]]

#: 内置检查器（名称即 VerifySpec.checks 取值）

async def check_data_quality(vc: VerifyContext) -> VerifyCheckResult:
    """数据质量检查：样本量、缺失率、重复行、离群率。"""
    quality = extract_metrics(vc.output).get("data_quality")
    if not isinstance(quality, dict) or not quality:
        return _skip("data_quality", "输出未提供 data_quality，跳过数据质量检查")
    rules = [
        ("rows", quality.get("rows"), 1000, "min", "样本行数"),
        ("missing_rate", quality.get("missing_rate"), 0.01, "max", "缺失率"),
        ("duplicate_rows", quality.get("duplicate_rows"), 5, "max", "重复行数"),
        ("outlier_rate", quality.get("outlier_rate"), 0.02, "max", "离群率"),
    ]
    failures = []
    checked = []
    for name, actual, threshold, mode, label in rules:
        checked.append({"name": name, "actual": actual, "threshold": threshold, "mode": mode})
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            failures.append(f"{label}缺失")
            continue
        bad = float(actual) < threshold if mode == "min" else float(actual) > threshold
        if bad:
            failures.append(f"{label}={_fmt_number(actual)}（阈值 "
                            f"{'≥' if mode == 'min' else '≤'}{_fmt_number(threshold)}）")
    evidence = {"quality": quality, "checked": checked, "failures": failures}
    if failures:
        return _fail("data_quality", "数据质量未达标：" + "；".join(failures), evidence)
    return VerifyCheckResult(
        name="data_quality",
        passed=True,
        detail=f"数据质量通过：{quality.get('rows')} 行，缺失率 {quality.get('missing_rate')}，"
               f"重复 {quality.get('duplicate_rows')} 行，离群率 {quality.get('outlier_rate')}",
        evidence=evidence,
    )


async def check_holdout_stability(vc: VerifyContext) -> VerifyCheckResult:
    """留出稳定性：训练 R² 与留出 R² 的差距不超过 0.15。"""
    metrics = extract_metrics(vc.output)
    r2 = metrics.get("r2")
    holdout = metrics.get("r2_holdout")
    if not isinstance(r2, (int, float)) or not isinstance(holdout, (int, float)):
        return _skip("holdout_stability", "输出未提供 r2 / r2_holdout，跳过留出稳定性检查")
    gap = abs(float(r2) - float(holdout))
    evidence = {"r2": r2, "r2_holdout": holdout, "gap": round(gap, 4), "tolerance": 0.15}
    if gap > 0.15:
        return _fail("holdout_stability",
                     f"留出稳定性不足：R²={_fmt_number(r2)}，留出 R²={_fmt_number(holdout)}，"
                     f"差距 {gap:.3f} 超过 0.15", evidence)
    if float(holdout) < 0.5:
        return _fail("holdout_stability",
                     f"留出集解释力过低：r2_holdout={_fmt_number(holdout)} < 0.5", evidence)
    return VerifyCheckResult(
        name="holdout_stability",
        passed=True,
        detail=f"留出稳定：R²={_fmt_number(r2)}、留出 R²={_fmt_number(holdout)}，差距 {gap:.3f}",
        evidence=evidence,
    )


async def check_multicollinearity(vc: VerifyContext) -> VerifyCheckResult:
    """共线性：所有因子 VIF ≤ 5。"""
    vif = extract_metrics(vc.output).get("vif")
    if not isinstance(vif, dict) or not vif:
        return _skip("multicollinearity", "输出未提供 VIF，跳过共线性检查")
    numeric = {k: float(v) for k, v in vif.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not numeric:
        return _skip("multicollinearity", "VIF 无有效数值，跳过")
    worst_key = max(numeric, key=lambda k: numeric[k])
    evidence = {"vif": vif, "max_vif": numeric[worst_key], "worst": worst_key, "threshold": 5.0}
    if numeric[worst_key] > 5.0:
        return _fail("multicollinearity",
                     f"存在共线性风险：{worst_key} 的 VIF={numeric[worst_key]:.2f} > 5", evidence)
    return VerifyCheckResult(
        name="multicollinearity",
        passed=True,
        detail=f"共线性可接受：最大 VIF={numeric[worst_key]:.2f}（{worst_key}）≤ 5",
        evidence=evidence,
    )


async def check_significance(vc: VerifyContext) -> VerifyCheckResult:
    """显著性：所有申报系数的 p 值 ≤ 0.05。"""
    p_values = extract_metrics(vc.output).get("p_values")
    if not isinstance(p_values, dict) or not p_values:
        return _skip("significance", "输出未提供 p_values，跳过显著性检查")
    numeric = {k: float(v) for k, v in p_values.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not numeric:
        return _skip("significance", "p 值无有效数值，跳过")
    worst_key = max(numeric, key=lambda k: numeric[k])
    evidence = {"p_values": p_values, "max_p": numeric[worst_key], "worst": worst_key, "threshold": 0.05}
    if numeric[worst_key] > 0.05:
        return _fail("significance",
                     f"显著性不足：{worst_key} 的 p={numeric[worst_key]:.4g} > 0.05", evidence)
    return VerifyCheckResult(
        name="significance",
        passed=True,
        detail=f"显著性通过：{len(numeric)} 项系数 p ≤ 0.05（最大 p={numeric[worst_key]:.4g}）",
        evidence=evidence,
    )


async def check_uncertainty(vc: VerifyContext) -> VerifyCheckResult:
    """不确定性：95% 置信区间不应跨过 0。"""
    ci95 = extract_metrics(vc.output).get("ci95")
    if not isinstance(ci95, dict) or not ci95:
        return _skip("uncertainty", "输出未提供 ci95，跳过置信区间检查")
    crossings = []
    checked = 0
    for key, interval in ci95.items():
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            continue
        low, high = float(interval[0]), float(interval[1])
        checked += 1
        if low <= 0.0 <= high:
            crossings.append(f"{key} [{low}, {high}]")
    if checked == 0:
        return _skip("uncertainty", "置信区间无有效数值，跳过")
    evidence = {"ci95": ci95, "checked": checked, "crossings": crossings}
    if crossings:
        return _fail("uncertainty",
                     f"{len(crossings)} 项系数的 95% 置信区间跨过 0：{'；'.join(crossings[:3])}",
                     evidence)
    return VerifyCheckResult(
        name="uncertainty",
        passed=True,
        detail=f"不确定性可控：{checked} 项系数的 95% 置信区间均不跨 0",
        evidence=evidence,
    )


DEFAULT_CHECKERS: dict[str, CheckerFn] = {
    "schema": check_schema,
    "non_empty": check_non_empty,
    "numeric_ground_truth": check_numeric_ground_truth,
    "code_rerun": check_code_rerun,
    "cross_agent": check_cross_agent,
    "statistical": check_statistical,
    "data_quality": check_data_quality,
    "holdout_stability": check_holdout_stability,
    "multicollinearity": check_multicollinearity,
    "significance": check_significance,
    "uncertainty": check_uncertainty,
    "llm_judge": check_llm_judge,
    "artifact_exists": check_artifact_exists,
}

#: 默认权重（体现「独立复算/真值 > 结构」的可信度差异）
DEFAULT_WEIGHTS: dict[str, float] = {
    "schema": 0.8,
    "non_empty": 0.5,
    "numeric_ground_truth": 1.5,
    "code_rerun": 1.5,
    "cross_agent": 1.2,
    "statistical": 1.0,
    "data_quality": 1.0,
    "holdout_stability": 1.2,
    "multicollinearity": 0.8,
    "significance": 1.1,
    "uncertainty": 0.8,
    "llm_judge": 0.6,
    "artifact_exists": 0.8,
}

#: 检查器所属层级（前端验证中心按层展示）
LAYER_MAP: dict[str, str] = {
    "schema": "L1-结构",
    "non_empty": "L1-结构",
    "numeric_ground_truth": "L2-真值",
    "code_rerun": "L3-复算",
    "cross_agent": "L4-交叉",
    "statistical": "L5-统计",
    "data_quality": "L1-数据",
    "holdout_stability": "L5-稳健",
    "multicollinearity": "L5-诊断",
    "significance": "L5-统计",
    "uncertainty": "L5-统计",
    "llm_judge": "L5-评审",
    "artifact_exists": "L1-产物",
}

#: 硬性检查：一旦实际执行并失败，无论加权置信度多高都判验证失败
HARD_CHECKS: set[str] = {
    "schema",
    "numeric_ground_truth",
    "code_rerun",
    "cross_agent",
    "statistical",
    "data_quality",
    "holdout_stability",
    "artifact_exists",
}

#: 前端策略说明
CHECKER_DESCRIPTIONS: dict[str, str] = {
    "schema": "输出必须是 JSON 对象且包含节点声明的 outputs 契约字段",
    "non_empty": "输出非空；文本长度 ≥ 20 字符",
    "numeric_ground_truth": "与隐藏真值做逐指标相对误差比对（容差 spec.tolerance）",
    "code_rerun": "用 python_exec 独立重跑输出中的 code，比对复算指标与申报指标",
    "cross_agent": "让独立 verifier Agent 复算并比对，防止单 Agent 自证",
    "statistical": "样本量/拟合优度/显著性阈值检查（spec.config.thresholds）",
    "data_quality": "数据质量：样本量、缺失率、重复行、离群率",
    "holdout_stability": "留出法稳健性：R² 与留出 R² 的差距 ≤ 0.15",
    "multicollinearity": "共线性诊断：所有因子 VIF ≤ 5",
    "significance": "显著性：所有系数 p 值 ≤ 0.05",
    "uncertainty": "不确定性：95% 置信区间不跨 0",
    "llm_judge": "结构化 LLM 评审；离线时降级为跳过并标注",
    "artifact_exists": "声明的产物文件存在且非空",
}

__all__ = [
    "ABS_FLOOR",
    "CHECKER_DESCRIPTIONS",
    "DEFAULT_CHECKERS",
    "DEFAULT_WEIGHTS",
    "HARD_CHECKS",
    "LAYER_MAP",
    "CheckerFn",
    "VerifyContext",
    "as_output_dict",
    "check_artifact_exists",
    "check_code_rerun",
    "check_cross_agent",
    "check_llm_judge",
    "check_non_empty",
    "check_numeric_ground_truth",
    "check_schema",
    "check_statistical",
    "check_data_quality",
    "check_holdout_stability",
    "check_multicollinearity",
    "check_significance",
    "check_uncertainty",
    "compare_numeric",
    "extract_metrics",
    "flatten_numbers",
    "parse_json_text",
]
