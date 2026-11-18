"""工作流领域层：数据模型、DSL 构造器与执行引擎。

对外导出
--------
* :class:`WorkflowStore` —— ``workflows/*.json`` 的加载/保存/校验仓库；
* :class:`WorkflowBuilder` —— 链式构造工作流的 DSL；
* :class:`WorkflowEngine` / :class:`EngineResult` —— 基于 asyncio 的执行引擎。
"""
from __future__ import annotations

from .dsl import WorkflowBuilder
from .engine import EngineResult, NodeExecutionError, WorkflowEngine
from .model import DEFAULT_WORKFLOW_DIR, WorkflowStore

__all__ = [
    "DEFAULT_WORKFLOW_DIR",
    "EngineResult",
    "NodeExecutionError",
    "WorkflowBuilder",
    "WorkflowEngine",
    "WorkflowStore",
]
