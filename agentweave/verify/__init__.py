"""可信验证子系统。

对外导出 ``VerificationPipeline`` / ``VerifyContext`` 与内置检查器注册表，
Web 层可直接用 ``pipeline.describe()`` 渲染「验证中心」。
"""
from __future__ import annotations

from .checkers import (
    CHECKER_DESCRIPTIONS,
    DEFAULT_CHECKERS,
    DEFAULT_WEIGHTS,
    HARD_CHECKS,
    LAYER_MAP,
    VerifyContext,
    check_artifact_exists,
    check_code_rerun,
    check_cross_agent,
    check_llm_judge,
    check_non_empty,
    check_numeric_ground_truth,
    check_schema,
    check_statistical,
)
from .pipeline import VerificationPipeline

__all__ = [
    "CHECKER_DESCRIPTIONS",
    "DEFAULT_CHECKERS",
    "DEFAULT_WEIGHTS",
    "HARD_CHECKS",
    "LAYER_MAP",
    "VerificationPipeline",
    "VerifyContext",
    "check_artifact_exists",
    "check_code_rerun",
    "check_cross_agent",
    "check_llm_judge",
    "check_non_empty",
    "check_numeric_ground_truth",
    "check_schema",
    "check_statistical",
]
