"""AgentWeave LLM 子包。

对外统一导出抽象基类、结果类型、错误分类、两种提供方实现与模型路由器，
上层（AgentRuntime / RuntimeContext / Web API）只依赖这里的名字。
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
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider, OpenAIProvider
from .router import ModelRouter

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
    "MockProvider",
    "OpenAICompatProvider",
    "OpenAIProvider",
    "ModelRouter",
]
