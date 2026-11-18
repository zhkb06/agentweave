"""工具层：注册表、内置工具与 MCP 客户端。

对外主要入口
------------
* :class:`ToolRegistry` / :class:`ToolResult`：工具注册、参数校验、超时与事件；
* :func:`build_tool_registry`：创建并注册全部内置工具（平台装配入口）；
* :class:`MCPClient` / :class:`MCPManager`：stdio JSON-RPC 2.0 的 MCP 客户端。

内置工具（名字见 docs/CONTRACTS.md §3.2）：``python_exec``、``calculator``、
``dataset_stats``、``chart_svg``、``file_read``、``file_write``、``http_get``、
``json_query``、``report_render``。
"""
from __future__ import annotations

from .builtin import (
    ALLOWED_IMPORTS,
    MAX_OUTPUT_BYTES,
    build_tool_registry,
    check_code_safety,
    register_builtin_tools,
)
from .mcp_client import (
    MCP_PROTOCOL_VERSION,
    MCPClient,
    MCPError,
    MCPManager,
    load_manager_from_config,
)
from .registry import ToolRegistry, ToolResult, validate_args

__all__ = [
    "ToolRegistry",
    "ToolResult",
    "validate_args",
    "build_tool_registry",
    "register_builtin_tools",
    "check_code_safety",
    "ALLOWED_IMPORTS",
    "MAX_OUTPUT_BYTES",
    "MCPClient",
    "MCPManager",
    "MCPError",
    "MCP_PROTOCOL_VERSION",
    "load_manager_from_config",
]
