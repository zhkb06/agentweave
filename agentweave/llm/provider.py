"""LLM 提供方兼容入口（真实实现见 base.py / openai_compat.py）。

历史/用户约定里存在 ``agentweave.llm.provider`` 这个模块名，这里统一再导出，
保证 ``from agentweave.llm.provider import LLMProvider, OpenAICompatProvider``
两种写法都成立；实现本身维护在 CONTRACTS 指定的
``agentweave/llm/base.py`` 与 ``agentweave/llm/openai_compat.py``。
"""
from __future__ import annotations

from .base import (
    DEFAULT_API_KEY_ENV,
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMNetworkError,
    LLMProvider,
    LLMRateLimitError,
    LLMResult,
    LLMServerError,
    LLMTimeoutError,
    LLMUsage,
)
from .openai_compat import OpenAICompatProvider, OpenAIProvider

__all__ = [
    "DEFAULT_API_KEY_ENV",
    "LLMUsage",
    "LLMResult",
    "LLMError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMNetworkError",
    "LLMProvider",
    "OpenAICompatProvider",
    "OpenAIProvider",
]
