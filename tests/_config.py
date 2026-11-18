"""测试专用配置：强制离线 Mock，避免误用真实 LLM 产生费用/网络依赖。"""

from __future__ import annotations

from typing import Any

from agentweave.runtime.platform import load_platform_config


def mock_platform_config() -> dict[str, Any]:
    """返回一份 ``provider=mock`` 的平台配置副本，供集成测试使用。"""
    cfg = dict(load_platform_config())
    llm = dict(cfg.get("llm") or {})
    llm["provider"] = "mock"
    llm["model"] = "mock-deterministic"
    llm.pop("api_key", None)
    llm.pop("reasoning_effort", None)
    llm["api_key_env"] = ""
    cfg["llm"] = llm
    return cfg
