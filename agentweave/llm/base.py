"""LLM 提供方抽象与统一结果类型。

本模块只依赖 Python 标准库，是 LLM 子包的最底层契约：

* :class:`LLMUsage` 记录 token 用量；
* :class:`LLMResult` 是 Agent 运行时与 ModelRouter 共享的调用结果；
* :class:`LLMProvider` 是抽象基类（鸭子类型兼容 CONTRACTS 中的 Protocol）；
* 错误分类 :class:`LLMError` 系列，带 ``retryable`` 标记供路由器决策。

安全约定：本模块不接触 API Key，Key 只存在于 :mod:`agentweave.llm.openai_compat`，
且从不写入日志或异常信息。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

#: 默认的 API Key 环境变量名（不硬编码任何真实 Key）。
DEFAULT_API_KEY_ENV = "AGENTWEAVE_API_KEY"


# ---------------------------------------------------------------------------
# 结果与用量
# ---------------------------------------------------------------------------
@dataclass
class LLMUsage:
    """一次（或累计）LLM 调用的 token 用量。"""

    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def total(self) -> int:
        """输入 + 输出 token 总数。"""
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict[str, int]:
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "total": self.total}


@dataclass
class LLMResult:
    """统一的 LLM 调用结果。

    契约字段见 docs/CONTRACTS.md §3.2；``provider`` / ``error`` 为附加字段，
    均带默认值，不影响按位置构造的兼容性。
    """

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"id","name","arguments"(dict)}]
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_s: float = 0.0
    raw: dict[str, Any] | None = None
    provider: str = ""
    error: str = ""

    @property
    def usage(self) -> LLMUsage:
        return LLMUsage(self.tokens_in, self.tokens_out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": list(self.tool_calls),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "latency_s": round(self.latency_s, 4),
            "provider": self.provider,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """LLM 调用错误基类；``retryable`` 表示是否值得重试。"""

    retryable: bool = False

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMAuthError(LLMError):
    """鉴权失败（401/403），不重试。"""


class LLMBadRequestError(LLMError):
    """请求非法（400/404/422），不重试。"""


class LLMRateLimitError(LLMError):
    """限流/配额（429/408），可重试。"""

    retryable = True


class LLMServerError(LLMError):
    """服务端错误（>=500），可重试。"""

    retryable = True


class LLMTimeoutError(LLMError):
    """网络超时，可重试。"""

    retryable = True


class LLMNetworkError(LLMError):
    """网络不可达 / DNS 失败等，可重试。"""

    retryable = True


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class LLMProvider(abc.ABC):
    """LLM 提供方抽象基类（鸭子类型兼容 CONTRACTS 中的 Protocol）。"""

    name: str = "provider"

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: str | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        """执行一次对话补全。"""

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        """列出可用模型名。"""


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
]
