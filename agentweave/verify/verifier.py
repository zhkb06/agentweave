"""验证流水线与加权置信度汇总。

职责
----
* 按 ``VerifySpec.checks`` 顺序执行检查器，单项异常不会中断整个验证；
* 汇总加权通过率得到 ``confidence``，并结合硬性失败判定 ``passed``；
* 把失败项整理成**中文、可执行**的结构化反馈，供 PDCA 自循环注入下一轮 prompt；
* 追加 ``run.verification`` 记录并发出 ``verify.result`` 事件（前端验证中心消费）。

检查器注册表可扩展：``pipeline.register("my_check", fn)``；内置实现见
:mod:`agentweave.verify.checkers`。
"""
from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from ..core.types import EventType, VerifyCheckResult, VerifyResult, VerifySpec, WorkflowNode
from .checkers import (
    CHECKER_DESCRIPTIONS,
    DEFAULT_CHECKERS,
    DEFAULT_WEIGHTS,
    HARD_CHECKS,
    LAYER_MAP,
    CheckerFn,
    VerifyContext,
)


class VerificationPipeline:
    """可扩展的验证流水线（默认注册五层八种内置检查器）。"""

    def __init__(
        self, ctx_provider: Callable[[], Any] | None = None
    ) -> None:
        self._ctx_provider = ctx_provider
        self._static_ctx: Any = None
        self._checkers: dict[str, CheckerFn] = dict(DEFAULT_CHECKERS)
        self._weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self._descriptions: dict[str, str] = dict(CHECKER_DESCRIPTIONS)
        self._order: list[str] = list(DEFAULT_CHECKERS)

    # ------------------------------------------------------------------
    # 注册 / 查询
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        checker: Callable[[VerifyContext], Awaitable[VerifyCheckResult]],
        weight: float = 1.0,
        description: str = "",
    ) -> None:
        """注册（或覆盖）一个检查器；``weight`` 参与加权置信度计算。"""
        key = str(name).strip()
        if not key:
            raise ValueError("检查器名称不能为空")
        if not callable(checker):
            raise TypeError("checker 必须可调用")
        self._checkers[key] = checker
        self._weights[key] = max(0.0, float(weight))
        if description:
            self._descriptions[key] = description
        if key not in self._order:
            self._order.append(key)

    def unregister(self, name: str) -> bool:
        """移除检查器；内置检查器也可移除（测试/定制场景）。"""
        removed = self._checkers.pop(name, None) is not None
        self._weights.pop(name, None)
        if name in self._order:
            self._order.remove(name)
        return removed

    def available(self) -> list[str]:
        """当前可用检查器名称（按注册顺序）。"""
        return [name for name in self._order if name in self._checkers]

    def describe(self) -> list[dict[str, Any]]:
        """检查器说明（供 ``GET /api/verify/strategies``）。"""
        return [
            {
                "name": name,
                "description": self._descriptions.get(name, ""),
                "weight": round(self._weights.get(name, 1.0), 3),
                "layer": LAYER_MAP.get(name, "custom"),
                "hard": name in HARD_CHECKS,
                "builtin": name in DEFAULT_CHECKERS,
            }
            for name in self.available()
        ]

    def set_context(self, ctx: Any) -> None:
        """绑定运行时上下文（不传 ``ctx_provider`` 时使用）。"""
        self._static_ctx = ctx

    def _runtime_ctx(self) -> Any:
        if self._ctx_provider is not None:
            try:
                return self._ctx_provider()
            except Exception:
                return None
        return self._static_ctx

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------
    async def verify(
        self,
        spec: VerifySpec,
        node: WorkflowNode,
        output: Any,
        run: Any,
    ) -> VerifyResult:
        """执行 ``spec.checks`` 并返回加权验证结果。"""
        spec = spec or VerifySpec()
        ctx = self._runtime_ctx()
        vc = VerifyContext(node=node, output=output, run=run, spec=spec, ctx=ctx)
        checks: list[VerifyCheckResult] = []
        total_weight = 0.0
        passed_weight = 0.0
        hard_fail = False
        executed_layers: list[str] = []

        for name in list(spec.checks or []):
            checker = self._checkers.get(name)
            base_weight = max(0.0, float(self._weights.get(name, 1.0)))
            if checker is None:
                result = VerifyCheckResult(
                    name=str(name),
                    passed=False,
                    detail=f"未知检查器: {name}（可用: {', '.join(self.available())}）",
                    weight=base_weight,
                    evidence={
                        "expected": "已注册的检查器名称",
                        "actual": str(name),
                        "suggestion": "检查 VerifySpec.checks 拼写或先注册检查器",
                    },
                )
                hard_fail = True
            else:
                try:
                    result = await checker(vc)
                except Exception as exc:  # 单项检查异常不阻断整条流水线
                    result = VerifyCheckResult(
                        name=str(name),
                        passed=False,
                        detail=f"检查器执行异常：{type(exc).__name__}: {exc}",
                        weight=base_weight,
                        evidence={
                            "error": f"{type(exc).__name__}: {exc}",
                            "suggestion": "检查输入数据结构或检查器实现",
                        },
                    )
            if not isinstance(result, VerifyCheckResult):
                result = VerifyCheckResult(
                    name=str(name),
                    passed=bool(result),
                    detail="检查器返回值被强制归一化",
                    weight=base_weight,
                )
            result.name = result.name or str(name)
            # 有效权重 = 注册权重 × 检查器自报权重（条款级置信度）
            effective = base_weight * (result.weight if result.weight else 1.0)
            result.weight = round(effective, 4)
            skipped = bool(result.evidence.get("skipped"))
            checks.append(result)
            if skipped:
                continue
            total_weight += effective
            if result.passed:
                passed_weight += effective
            else:
                if self._is_hard(name):
                    hard_fail = True
                layer = LAYER_MAP.get(name, "custom")
                if layer not in executed_layers:
                    executed_layers.append(layer)

        confidence = round(passed_weight / total_weight, 4) if total_weight > 0 else 0.0
        passed = bool(checks) and total_weight > 0 and confidence >= float(
            spec.min_confidence
        ) and not hard_fail
        feedback = "" if passed else self._build_feedback(checks, confidence, spec, hard_fail)
        layer = self._highest_layer(executed_layers)
        result = VerifyResult(
            passed=passed,
            confidence=confidence,
            checks=checks,
            feedback=feedback,
            metrics={
                "checks_total": len(checks),
                "checks_failed": sum(
                    1
                    for c in checks
                    if not c.passed and not c.evidence.get("skipped")
                ),
                "checks_skipped": sum(
                    1 for c in checks if c.evidence.get("skipped")
                ),
                "hard_fail": hard_fail,
                "min_confidence": float(spec.min_confidence),
                "confidence": confidence,
                "layers": executed_layers,
            },
            layer=layer,
        )
        self._record(result, node, run, ctx)
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _is_hard(name: str) -> bool:
        return name in HARD_CHECKS

    @staticmethod
    def _highest_layer(layers: list[str]) -> str:
        """取执行过的最高验证层（L5 > L4 > ...）。"""
        if not layers:
            return "auto"
        best = "auto"
        best_rank = -1
        for layer in layers:
            digits = "".join(ch for ch in layer if ch.isdigit())
            rank = int(digits) if digits else 0
            if rank > best_rank:
                best, best_rank = layer, rank
        return best

    @staticmethod
    def _build_feedback(
        checks: list[VerifyCheckResult],
        confidence: float,
        spec: VerifySpec,
        hard_fail: bool,
    ) -> str:
        """把失败检查整理为中文可执行反馈（自循环直接注入 prompt）。"""
        failed = [
            c
            for c in checks
            if not c.passed and not c.evidence.get("skipped")
        ]
        head = (
            f"【验证未通过】加权置信度 {confidence:.2f} 低于阈值 "
            f"{float(spec.min_confidence):.2f}"
        )
        if hard_fail:
            head += "，且存在硬性失败项"
        lines: list[str] = [head + "。"]
        for index, check in enumerate(failed, start=1):
            detail = check.detail or "未通过"
            lines.append(f"{index}. {check.name}：{detail}")
            evidence = check.evidence or {}
            expected = evidence.get("expected")
            actual = evidence.get("actual")
            if expected is not None:
                lines.append(f"   期望：{_short_json(expected)}")
            if actual is not None:
                lines.append(f"   实际：{_short_json(actual)}")
            failures = evidence.get("failures")
            if isinstance(failures, list) and failures and not isinstance(expected, dict):
                lines.append(f"   差异明细：{_short_json(failures)}")
            suggestion = evidence.get("suggestion")
            if suggestion:
                lines.append(f"   建议：{suggestion}")
        skipped = [c for c in checks if c.evidence.get("skipped")]
        if skipped:
            lines.append(
                "已跳过（不可用）："
                + "、".join(f"{c.name}（{c.detail}）" for c in skipped)
            )
        lines.append("请仅针对上述问题修正后重新输出完整 JSON，不要重复已通过的字段说明。")
        return "\n".join(lines)

    def _record(
        self, result: VerifyResult, node: WorkflowNode, run: Any, ctx: Any
    ) -> None:
        """追加运行级验证记录并发布事件（任何异常都不得影响主流程）。"""
        record = {
            "node_id": node.id,
            "ts": time.time(),
            "passed": result.passed,
            "confidence": result.confidence,
            "layer": result.layer,
            "checks": [c.to_dict() for c in result.checks],
            "feedback": result.feedback,
            "metrics": result.metrics,
        }
        if run is not None:
            try:
                if hasattr(run, "add_verification"):
                    run.add_verification(record)
                else:  # 兼容极简 RunState 实现
                    run.verification.append(record)  # type: ignore[attr-defined]
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.emit(
                    EventType.VERIFY_RESULT,
                    run_id=getattr(run, "run_id", "") or getattr(ctx, "run_id", ""),
                    node_id=node.id,
                    payload={
                        "node_id": node.id,
                        "passed": result.passed,
                        "confidence": result.confidence,
                        "layer": result.layer,
                        "checks": [
                            {
                                "name": c.name,
                                "passed": c.passed,
                                "detail": c.detail,
                                "weight": c.weight,
                                "skipped": bool(c.evidence.get("skipped")),
                            }
                            for c in result.checks
                        ],
                    },
                )
            except Exception:
                pass


def _short_json(value: Any, limit: int = 500) -> str:
    """压缩 JSON 用于反馈文本，避免超长 prompt。"""
    if isinstance(value, str):
        return value[:limit]
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["VerificationPipeline", "VerifyContext", "CheckerFn"]
