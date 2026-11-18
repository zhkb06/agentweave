"""模型路由器：fast / smart 分层路由 + 失败重试 + 离线 Mock 回退 + 用量统计。

职责
----
* 读取 ``config/platform.json`` 的 ``llm`` 配置（也接受完整平台配置），
  按 ``llm.router`` 中的角色映射把请求路由到 ``fast`` / ``smart`` 两档模型；
* ``provider=mock`` 时使用 :class:`MockProvider`，保证无网络可离线演示；
  ``provider`` 为任意 OpenAI 兼容服务时使用 :class:`OpenAICompatProvider`；
* 调用失败按指数退避重试（默认 2 次），最终回退到离线 Mock，保证 Demo 不中断；
* 记录各模型调用次数、token、平均延迟，并发出 ``llm.called`` / ``llm.result`` 事件
  （payload 只含模型与用量，不含 prompt 全文）；
* ``configure()`` / ``status()`` / ``usage()`` 永不返回 API Key 明文。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from typing import Any

from ..core.types import EventType
from .base import LLMError, LLMProvider, LLMResult
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

_LOGGER = logging.getLogger(__name__)

#: 视为“OpenAI 兼容”的提供方名称。
_OPENAI_LIKE = {"openai", "openai_compat", "deepseek", "qwen", "zhipu", "custom", "azure"}


class ModelRouter:
    """按角色分层的模型路由器（契约见 docs/CONTRACTS.md §3.2）。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        providers: dict[str, LLMProvider] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.bus: Any = None
        self._cfg: dict[str, Any] = {}
        self._tier_providers: dict[str, LLMProvider] = {}
        self._fallback_provider = MockProvider(model="mock-fallback")
        self._stats: dict[str, Any] = {
            "calls": 0,
            "attempts": 0,
            "errors": 0,
            "fallback_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "latency_s": 0.0,
            "by_model": {},
        }
        if providers:
            self._tier_providers = dict(providers)
        self.configure(config or {})

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _unwrap(config: dict[str, Any]) -> dict[str, Any]:
        """兼容传入完整 platform.json：优先取其中的 ``llm`` 字典。"""
        if isinstance(config.get("llm"), dict):
            return dict(config["llm"])
        return dict(config)

    def configure(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """运行时切换模型配置，返回脱敏后的配置（不含 Key 明文）。"""
        raw = self._unwrap(cfg or {})
        with self._lock:
            merged = dict(self._cfg)
            merged.update(raw)
            # 兼容平台配置中的命名
            if "timeout_s" in merged and "timeout" not in merged:
                merged["timeout"] = merged["timeout_s"]
            self._cfg = merged
            if not self._tier_providers or raw:
                self._build_providers()
            return self.sanitized_config()

    def _build_providers(self) -> None:
        cfg = self._cfg
        provider_name = str(cfg.get("provider", "mock") or "mock").lower()
        base_url = str(cfg.get("base_url", "https://api.openai.com/v1") or "")
        model = str(cfg.get("model", "gpt-4o-mini") or "")
        fast_model = str(cfg.get("fast_model") or model)
        smart_model = str(cfg.get("smart_model") or model)
        timeout = float(cfg.get("timeout", cfg.get("timeout_s", 60)) or 60)
        api_key_env = str(cfg.get("api_key_env", "AGENTWEAVE_API_KEY"))

        if provider_name in _OPENAI_LIKE:
            smart = OpenAICompatProvider(
                base_url=base_url,
                api_key=cfg.get("api_key") or None,
                model=smart_model,
                timeout=timeout,
                max_retries=0,  # 重试交给路由器统一处理
                api_key_env=api_key_env,
            )
            fast = OpenAICompatProvider(
                base_url=base_url,
                api_key=cfg.get("api_key") or None,
                model=fast_model,
                timeout=timeout,
                max_retries=0,
                api_key_env=api_key_env,
            )
            self._tier_providers = {"fast": fast, "smart": smart}
            # 未配置 Key 时主动降级为 Mock，保证离线演示可用
            if not smart.has_key:
                self._tier_providers = {
                    "fast": MockProvider(model=f"mock-degraded-fast"),
                    "smart": MockProvider(model=f"mock-degraded-smart"),
                }
                _LOGGER.warning("未配置 API Key，ModelRouter 已降级为离线 MockProvider")
        else:
            self._tier_providers = {
                "fast": MockProvider(model="mock-fast"),
                "smart": MockProvider(model="mock-smart"),
            }

    def sanitized_config(self) -> dict[str, Any]:
        """返回可安全展示的配置（Key 只以布尔值出现）。"""
        cfg = copy.deepcopy(self._cfg)
        api_key = cfg.pop("api_key", "")
        cfg.pop("api_key_env", None)  # 环境变量名本身不算敏感，但无需回显
        cfg["api_key_set"] = bool(api_key) or self.degraded is False and self._uses_remote
        cfg["degraded"] = self.degraded
        return cfg

    @property
    def _uses_remote(self) -> bool:
        return any(isinstance(p, OpenAICompatProvider) for p in self._tier_providers.values())

    @property
    def degraded(self) -> bool:
        """``provider`` 配置为远程但缺少 Key/已降级时为 True。"""
        provider = str(self._cfg.get("provider", "mock") or "mock").lower()
        if provider not in _OPENAI_LIKE:
            return False
        return not self._uses_remote

    def install_provider(self, tier: str, provider: LLMProvider) -> None:
        """注入/替换某一档的提供方（测试与高级扩展用）。"""
        if tier not in ("fast", "smart"):
            raise ValueError(f"未知模型档位: {tier}")
        with self._lock:
            self._tier_providers[tier] = provider

    def bind_bus(self, bus: Any) -> None:
        """绑定事件总线（可选）；绑定后每次调用发 llm.called / llm.result。"""
        self.bus = bus

    def set_bus(self, bus: Any) -> None:
        self.bind_bus(bus)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def tier_for(self, role: str) -> str:
        """按 ``llm.router`` 映射得到档位（fast / smart）。

        同时兼容 ``router`` 与 ``routing`` 两种配置键名（平台配置曾用过 ``routing``）。
        """
        mapping = self._cfg.get("router") or self._cfg.get("routing") or {}
        if not isinstance(mapping, dict):
            mapping = {}
        tier = str(mapping.get(role or "", mapping.get("default", "smart")) or "smart").lower()
        return tier if tier in self._tier_providers else "smart"

    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        role: str = "",
        **kwargs: Any,
    ) -> LLMResult:
        """路由并执行一次补全；失败重试后回退离线 Mock。"""
        run_id = str(kwargs.pop("run_id", "") or "")
        node_id = kwargs.pop("node_id", None)
        agent_id = kwargs.pop("agent_id", None)
        temperature = float(kwargs.pop("temperature", 0.2))
        max_tokens = int(kwargs.pop("max_tokens", 2048))
        response_format = kwargs.pop("response_format", None)

        with self._lock:
            tier = self.tier_for(role)
            provider = self._tier_providers.get(tier) or self._tier_providers.get("smart") or self._fallback_provider
            max_retries = max(0, int(self._cfg.get("max_retries", 2) or 0))

        started = time.perf_counter()
        self._emit(
            EventType.LLM_CALLED,
            run_id,
            node_id,
            agent_id,
            {
                "provider": provider.name,
                "model": self._provider_model(provider),
                "role": role,
                "tier": tier,
                "tools": len(tools or []),
            },
        )

        last_error: Exception | None = None
        fallback_used = False
        result: LLMResult | None = None
        for attempt in range(max_retries + 1):
            with self._lock:
                self._stats["attempts"] += 1
            try:
                result = await provider.complete(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                break
            except Exception as exc:  # noqa: BLE001 - 路由层必须兜底，不让一次调用炸掉工作流
                last_error = exc
                retryable = exc.retryable if isinstance(exc, LLMError) else True
                if not retryable or attempt >= max_retries:
                    break
                delay = min(2.0, 0.2 * (2 ** attempt))
                _LOGGER.warning(
                    "模型调用失败（role=%s, tier=%s, 第 %d 次重试, %.2fs）：%s",
                    role, tier, attempt + 1, delay, exc,
                )
                await asyncio.sleep(delay)

        if result is None:
            # 回退离线 Mock：保证 Demo / 工作流不中断
            with self._lock:
                self._stats["errors"] += 1
                self._stats["fallback_calls"] += 1
            _LOGGER.warning("模型调用最终失败，已回退离线 MockProvider：%s", last_error)
            try:
                result = await self._fallback_provider.complete(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                result.model = "mock-fallback"
                result.raw = dict(result.raw or {}, fallback=True, error=str(last_error or ""))
                fallback_used = True
            except Exception as exc:  # pragma: no cover - Mock 本身不应失败
                result = LLMResult(content="", model="mock-fallback", error=str(exc), provider="mock")
                fallback_used = True

        latency = time.perf_counter() - started
        result.latency_s = max(float(result.latency_s or 0.0), latency)
        self._record(result, fallback=fallback_used)
        self._emit(
            EventType.LLM_RESULT,
            run_id,
            node_id,
            agent_id,
            {
                "provider": result.provider or provider.name,
                "model": result.model,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "latency_s": round(result.latency_s, 4),
                "fallback": fallback_used,
                "ok": not fallback_used,
            },
        )
        return result

    # ------------------------------------------------------------------
    # 统计 / 状态
    # ------------------------------------------------------------------
    def _record(self, result: LLMResult, fallback: bool = False) -> None:
        with self._lock:
            stats = self._stats
            stats["calls"] += 1
            stats["tokens_in"] += int(result.tokens_in or 0)
            stats["tokens_out"] += int(result.tokens_out or 0)
            stats["latency_s"] += float(result.latency_s or 0.0)
            model = result.model or "unknown"
            entry = stats["by_model"].setdefault(
                model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "latency_s": 0.0, "fallback": 0}
            )
            entry["calls"] += 1
            entry["tokens_in"] += int(result.tokens_in or 0)
            entry["tokens_out"] += int(result.tokens_out or 0)
            entry["latency_s"] += float(result.latency_s or 0.0)
            if fallback:
                entry["fallback"] += 1

    def usage(self) -> dict[str, Any]:
        """返回累计用量（tokens、调用次数、平均延迟、分模型统计）。"""
        with self._lock:
            stats = self._stats
            calls = int(stats["calls"])
            by_model: dict[str, Any] = {}
            for model, entry in stats["by_model"].items():
                model_calls = int(entry["calls"])
                by_model[model] = {
                    "calls": model_calls,
                    "tokens_in": int(entry["tokens_in"]),
                    "tokens_out": int(entry["tokens_out"]),
                    "avg_latency_s": round(entry["latency_s"] / model_calls, 4) if model_calls else 0.0,
                    "fallback": int(entry.get("fallback", 0)),
                }
            return {
                "calls": calls,
                "attempts": int(stats["attempts"]),
                "errors": int(stats["errors"]),
                "fallback_calls": int(stats["fallback_calls"]),
                "tokens_in": int(stats["tokens_in"]),
                "tokens_out": int(stats["tokens_out"]),
                "total_tokens": int(stats["tokens_in"]) + int(stats["tokens_out"]),
                "avg_latency_s": round(stats["latency_s"] / calls, 4) if calls else 0.0,
                "by_model": by_model,
            }

    def status(self) -> dict[str, Any]:
        """健康状态与脱敏配置（前端“模型设置”页使用）。"""
        with self._lock:
            providers = {
                tier: {
                    "provider": provider.name,
                    "model": self._provider_model(provider),
                }
                for tier, provider in self._tier_providers.items()
            }
            usage = self.usage()
            return {
                "provider": str(self._cfg.get("provider", "mock")),
                "base_url": str(self._cfg.get("base_url", "")),
                "model": str(self._cfg.get("model", "")),
                "fast_model": str(self._cfg.get("fast_model") or self._cfg.get("model", "")),
                "smart_model": str(self._cfg.get("smart_model") or self._cfg.get("model", "")),
                "router": dict(self._cfg.get("router") or {}),
                "tiers": providers,
                "healthy": True,
                "degraded": self.degraded,
                "fallback_active": usage["fallback_calls"] > 0,
                "calls": usage["calls"],
                "errors": usage["errors"],
                "tokens_in": usage["tokens_in"],
                "tokens_out": usage["tokens_out"],
                "avg_latency_s": usage["avg_latency_s"],
                "usage": usage,
                "api_key_set": self._uses_remote and any(
                    getattr(p, "has_key", False) for p in self._tier_providers.values()
                ),
            }

    async def list_models(self) -> list[str]:
        """汇总各档提供方的可用模型列表（去重保序）。"""
        models: list[str] = []
        for provider in self._tier_providers.values():
            try:
                for model in await provider.list_models():
                    if model and model not in models:
                        models.append(model)
            except Exception as exc:  # noqa: BLE001 - 列表接口失败不影响主流程
                _LOGGER.warning("获取模型列表失败：%s", exc)
        return models

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _provider_model(provider: LLMProvider) -> str:
        return str(getattr(provider, "model", "") or provider.name)

    def _emit(
        self,
        type_: str,
        run_id: str,
        node_id: Any,
        agent_id: Any,
        payload: dict[str, Any],
    ) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                type_,
                run_id=run_id,
                node_id=node_id,
                agent_id=agent_id,
                payload=payload,
            )
        except Exception:  # pragma: no cover - 事件总线异常不能影响主流程
            _LOGGER.debug("发送 LLM 事件失败", exc_info=True)


__all__ = ["ModelRouter"]
