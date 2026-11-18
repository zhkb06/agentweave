"""智能体层：Agent 定义注册表、ReAct 运行时与运行级记忆。

对外主要入口
------------
* :class:`AgentRegistry` / :func:`load_agents`：加载并管理 ``config/agents.json``；
* :class:`AgentRuntime`：执行单个 Agent 的 ReAct 循环（推理 → 工具 → 观察 → 结论）；
* :class:`AgentMemory`：一次 run 内的轻量键值记忆与摘要。

执行引擎（``agentweave.runtime``）只通过 ``RuntimeContext`` 使用本包，
不直接依赖 LLM / 工具实现。
"""
from __future__ import annotations

from .agent import AgentRuntime, extract_json
from .memory import AgentMemory
from .registry import (
    DEFAULT_AGENTS_PATH,
    PROJECT_ROOT,
    AgentRegistry,
    get_default_registry,
    load_agents,
    reset_default_registry,
)

__all__ = [
    "AgentRuntime",
    "extract_json",
    "AgentMemory",
    "AgentRegistry",
    "load_agents",
    "get_default_registry",
    "reset_default_registry",
    "DEFAULT_AGENTS_PATH",
    "PROJECT_ROOT",
]
