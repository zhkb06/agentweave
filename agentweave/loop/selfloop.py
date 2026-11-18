"""PDCA 自循环控制器：验证不通过时把结构化反馈注入下一轮。

设计要点
--------
* **停止条件可解释**：验证通过即收敛；达到 ``spec.max_iterations`` 即耗尽；
  连续 ``STAGNATION_ROUNDS`` 轮置信度提升小于 ``STAGNATION_DELTA`` 判定停滞，
  避免「无意义重跑」烧 token；
* **反馈可执行**：优先复用验证流水线生成的中文 feedback（含失败项、期望/实际、
  建议），没有时从 ``VerifyResult.checks`` 证据重建；
* **状态可观测**：每轮记录 iteration/confidence/passed/feedback 预览，供前端
  「自循环」时间线展示。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..core.types import LoopSpec, VerifyResult


@dataclass
class LoopState:
    """一轮自循环的可变状态。"""

    iteration: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    converged: bool = False
    exhausted: bool = False
    best_confidence: float = 0.0
    last_confidence: float = 0.0
    feedback: str = ""
    stop_reason: str = "running"
    started_at: float = field(default_factory=time.time)


class SelfLoopController:
    """自循环策略：何时继续、如何生成反馈、如何记录历史。"""

    #: 单轮置信度提升低于该值视为「没有实质进展」
    STAGNATION_DELTA: float = 0.02
    #: 连续多少轮无实质进展后停止
    STAGNATION_ROUNDS: int = 2

    def __init__(self, spec: LoopSpec | None = None) -> None:
        self.spec = spec or LoopSpec()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def begin(self) -> LoopState:
        """开始一轮新的自循环，返回初始状态。"""
        return LoopState()

    def should_continue(self, verify: VerifyResult, state: LoopState) -> bool:
        """根据验证结果与历史判断是否继续下一轮修复。"""
        if verify.passed:
            state.converged = True
            state.stop_reason = "converged"
            return False
        if state.iteration >= max(1, int(self.spec.max_iterations)):
            state.exhausted = True
            state.stop_reason = "max_iterations"
            return False
        if self._stagnated(state):
            state.exhausted = True
            state.stop_reason = "stagnation"
            return False
        return True

    def build_feedback(self, verify: VerifyResult, state: LoopState) -> str:
        """构造注入下一轮 prompt 的中文反馈（含失败项与证据）。

        「第 N 轮修复请求」中的 N 表示**已失败轮次**（即第几次修复请求），
        因此调用方是否先 ``record`` 都从 1 开始计数，便于前端与日志阅读。
        """
        attempt = max(1, int(state.iteration))
        parts: list[str] = [
            f"【第 {attempt} 轮修复请求】上一轮验证未通过"
            f"（加权置信度 {verify.confidence:.2f}，阈值 "
            f"{verify.metrics.get('min_confidence', 'N/A')}）。"
        ]
        feedback = (verify.feedback or "").strip()
        if feedback:
            parts.append(feedback)
        else:
            failed = [
                check
                for check in verify.checks
                if not check.passed and not check.evidence.get("skipped")
            ]
            for index, check in enumerate(failed, start=1):
                parts.append(f"{index}. {check.name}：{check.detail}")
                evidence = check.evidence or {}
                if evidence.get("expected") is not None:
                    parts.append(f"   期望：{evidence['expected']}")
                if evidence.get("actual") is not None:
                    parts.append(f"   实际：{evidence['actual']}")
                if evidence.get("suggestion"):
                    parts.append(f"   建议：{evidence['suggestion']}")
        parts.append(
            "请仅针对上述失败项修正（重算指标/修正代码/补齐字段），"
            "保留已通过的结论，直接输出完整 JSON。"
        )
        text = "\n".join(parts)
        state.feedback = text
        return text

    def record(
        self,
        state: LoopState,
        verify: VerifyResult,
        output: Any = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """记录一轮验证结果；返回该轮历史条目。"""
        state.iteration += 1
        previous = state.last_confidence
        state.last_confidence = float(verify.confidence)
        state.best_confidence = max(state.best_confidence, float(verify.confidence))
        improvement = state.last_confidence - previous if state.history else 0.0
        entry: dict[str, Any] = {
            "iteration": state.iteration,
            "passed": bool(verify.passed),
            "confidence": round(float(verify.confidence), 4),
            "improvement": round(improvement, 4),
            "feedback": (verify.feedback or "")[:500],
            "failures": [
                check.name
                for check in verify.checks
                if not check.passed and not check.evidence.get("skipped")
            ],
            "output_preview": _preview(output),
            "ts": time.time(),
        }
        entry.update(extra)
        state.history.append(entry)
        if verify.passed:
            state.converged = True
            state.stop_reason = "converged"
        return entry

    def summary(self, state: LoopState | None) -> dict[str, Any]:
        """把循环状态压缩为可存储/可推送的摘要。"""
        if state is None:
            return {
                "iterations": 0,
                "iteration": 0,
                "max_iterations": int(self.spec.max_iterations),
                "converged": False,
                "exhausted": False,
                "stop_reason": "not_started",
                "history": [],
            }
        return {
            "iterations": state.iteration,
            "iteration": state.iteration,
            "max_iterations": int(self.spec.max_iterations),
            "converged": state.converged,
            "exhausted": state.exhausted,
            "stop_reason": state.stop_reason,
            "best_confidence": round(state.best_confidence, 4),
            "last_confidence": round(state.last_confidence, 4),
            "mode": self.spec.mode,
            "feedback_var": self.spec.feedback_var,
            "duration_s": round(time.time() - state.started_at, 4),
            "history": [
                {
                    "iteration": item["iteration"],
                    "passed": item["passed"],
                    "confidence": item["confidence"],
                    "improvement": item.get("improvement", 0.0),
                    "failures": item.get("failures", []),
                }
                for item in state.history
            ],
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _stagnated(self, state: LoopState) -> bool:
        """连续多轮置信度提升不足即判定停滞。"""
        recent = state.history[-self.STAGNATION_ROUNDS :]
        if len(recent) < self.STAGNATION_ROUNDS:
            return False
        improvements = [float(item.get("improvement", 0.0)) for item in recent]
        return all(delta < self.STAGNATION_DELTA for delta in improvements)


def _preview(output: Any, limit: int = 300) -> str:
    text = str(output)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["LoopState", "SelfLoopController"]
