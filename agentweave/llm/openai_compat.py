"""OpenAI 兼容聊天补全提供方（纯标准库 urllib 实现）。

支持任意 OpenAI 兼容接口（OpenAI / DeepSeek / 通义千问 / 智谱等）：

* ``POST {base_url}/chat/completions``，支持 Function Calling（tools）与 JSON mode；
* 超时、指数退避重试、错误分类（鉴权 / 限流 / 5xx / 网络 / 超时）；
* 记录 token 用量与调用延迟；
* API Key 只从构造参数或环境变量读取，日志与异常中都不出现 Key。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

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
)

_LOGGER = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """任意 OpenAI 兼容接口的提供方实现。

    参数
    ----
    base_url:
        接口根地址，例如 ``https://api.deepseek.com/v1``（末尾斜杠会被去掉）。
    api_key:
        显式 Key；为空时回退到 ``os.environ[api_key_env]``。
    model:
        默认模型名。
    timeout:
        单次 HTTP 请求超时（秒）。
    max_retries:
        失败重试次数（仅对可重试错误做指数退避）。
    backoff_base:
        指数退避基数（秒），单元测试可调小以加速。
    """

    name = "openai_compat"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        timeout: float = 60.0,
        max_retries: int = 2,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        backoff_base: float = 0.5,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> None:
        # ``timeout_s`` 是平台配置里的字段名；``**kwargs`` 只为向前兼容未知配置项。
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._api_key = (api_key or os.environ.get(api_key_env, "") or "").strip()
        self.model = model or ""
        self.timeout = float(timeout if timeout_s is None else timeout_s)
        self.extra_config: dict[str, Any] = dict(kwargs)
        # 思考型模型（如 GLM-5.3）可配置 reasoning_effort=low/high/max；
        # extra_body 用于透传厂商特有参数（原样合并进请求体）。
        self.reasoning_effort = str(self.extra_config.get("reasoning_effort") or "").strip()
        extra_body = self.extra_config.get("extra_body")
        self.extra_body: dict[str, Any] = dict(extra_body) if isinstance(extra_body, dict) else {}
        self.max_retries = max(0, int(max_retries))
        self.api_key_env = api_key_env
        self.backoff_base = max(0.0, float(backoff_base))
        self._logger = logging.getLogger(f"{__name__}.openai_compat")

    # -- 对外信息（不包含 Key） -----------------------------------------
    @property
    def has_key(self) -> bool:
        """是否已配置 API Key（只返回布尔值，绝不返回 Key 本身）。"""
        return bool(self._api_key)

    def describe(self) -> dict[str, Any]:
        """脱敏后的连接信息，供 API/前端展示。"""
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_s": self.timeout,
            "max_retries": self.max_retries,
            "api_key_set": self.has_key,
        }

    # -- 核心调用 --------------------------------------------------------
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: str | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        effective_max = int(max_tokens)
        if self.reasoning_effort:
            # 思考过程同样占用 max_tokens：给足输出预算，避免 content 为空
            effective_max = max(effective_max, 8192)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": effective_max,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.extra_body:
            payload.update(self.extra_body)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format in ("json", "json_object"):
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        data: dict[str, Any] | None = None
        last_error: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                data = await asyncio.to_thread(self._post_json, "/chat/completions", payload)
                break
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                delay = self.backoff_base * (2 ** attempt)
                self._logger.warning(
                    "LLM 调用失败（第 %d 次重试，%.2fs 后）：%s", attempt + 1, delay, exc
                )
                await asyncio.sleep(delay)
        if data is None:  # pragma: no cover - 防御性分支
            raise last_error or LLMError("LLM 调用失败且无响应")
        result = self._parse_result(data, started)
        # 思考型模型可能把输出预算全部用于思考，返回 finish_reason=length 且 content 为空；
        # 此时把 max_tokens 翻倍重试一次（上限 16384），避免整条链路因空内容失败。
        if (
            not result.content.strip()
            and not result.tool_calls
            and result.raw.get("finish_reason") == "length"
            and effective_max < 16384
        ):
            retry_max = min(16384, effective_max * 2)
            self._logger.warning(
                "模型返回空内容（疑似思考耗尽 max_tokens=%s），以 max_tokens=%s 重试一次",
                effective_max,
                retry_max,
            )
            payload["max_tokens"] = retry_max
            retry_data = await asyncio.to_thread(self._post_json, "/chat/completions", payload)
            return self._parse_result(retry_data, started)
        return result

    async def list_models(self) -> list[str]:
        """GET /models；失败返回空列表（不抛异常，便于健康检查）。"""
        try:
            data = await asyncio.to_thread(self._get_json, "/models")
        except LLMError as exc:
            self._logger.warning("获取模型列表失败：%s", exc)
            return []
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [str(item.get("id", "")) for item in items if isinstance(item, dict) and item.get("id")]

    # -- HTTP 底层 -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, payload)

    def _get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path, None)

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = self._http_error_detail(exc)
            raise self._classify_http_error(exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            message = f"LLM 网络请求失败：{reason}"
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LLMTimeoutError(message) from None
            raise LLMNetworkError(message) from None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise LLMBadRequestError("LLM 返回内容不是合法 JSON") from None
        if not isinstance(parsed, dict):
            raise LLMBadRequestError("LLM 返回 JSON 结构异常（期望对象）")
        return parsed

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        """读取错误响应体（截断，避免把大内容写进异常）。"""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return raw[:400]
        except Exception:  # pragma: no cover - 防御
            return ""

    @staticmethod
    def _classify_http_error(status_code: int, detail: str) -> LLMError:
        message = f"LLM HTTP {status_code}"
        if detail:
            message = f"{message}: {detail}"
        if status_code in (401, 403):
            return LLMAuthError(message, status_code)
        if status_code in (408, 409, 429):
            return LLMRateLimitError(message, status_code)
        if status_code >= 500:
            return LLMServerError(message, status_code)
        if 400 <= status_code < 500:
            return LLMBadRequestError(message, status_code)
        return LLMError(message, status_code)

    def _parse_result(self, data: dict[str, Any], started: float) -> LLMResult:
        """把 OpenAI 响应解析成 :class:`LLMResult`。"""
        choices = data.get("choices") or []
        message: dict[str, Any] = {}
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        tool_calls: list[dict[str, Any]] = []
        raw_calls = message.get("tool_calls") or []
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                arguments: Any = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"_raw": arguments[:2000]}
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_calls.append(
                    {
                        "id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "arguments": arguments,
                    }
                )

        usage = data.get("usage") or {}
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model") or self.model),
            latency_s=time.perf_counter() - started,
            raw={
                "id": str(data.get("id", "")),
                "model": str(data.get("model", "")),
                "finish_reason": (
                    str(choices[0].get("finish_reason", ""))
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                    else ""
                ),
            },
            provider=self.name,
        )


# 兼容别名
OpenAIProvider = OpenAICompatProvider

__all__ = ["OpenAICompatProvider", "OpenAIProvider"]
