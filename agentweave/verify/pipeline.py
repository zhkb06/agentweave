"""验证流水线兼容入口。

任务书要求文件名为 ``pipeline.py``，接口契约文件名为 ``verifier.py``；
实现统一放在 :mod:`agentweave.verify.verifier`，本模块仅做等价再导出，
两种 import 路径均可使用。
"""
from __future__ import annotations

from .verifier import VerificationPipeline, VerifyContext

__all__ = ["VerificationPipeline", "VerifyContext"]
