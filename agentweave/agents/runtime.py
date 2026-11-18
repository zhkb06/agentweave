"""Agent 运行时兼容入口。

真实实现在 :mod:`agentweave.agents.agent`（``RuntimeContext`` 与 ``docs/CONTRACTS.md``
均按该路径导入）。本模块仅作为 ``agents.runtime`` 旧路径的再导出，避免不同文档
（如任务板中提到的 ``agentweave/agents/runtime.py``）引用不一致导致导入失败。
"""
from __future__ import annotations

from .agent import (
    DEFAULT_MAX_ITERATIONS,
    MAX_CONTEXT_CHARS,
    MAX_OBSERVATION_CHARS,
    AgentRuntime,
    extract_json,
)

__all__ = [
    "AgentRuntime",
    "extract_json",
    "MAX_OBSERVATION_CHARS",
    "MAX_CONTEXT_CHARS",
    "DEFAULT_MAX_ITERATIONS",
]
