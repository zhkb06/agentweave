"""AgentWeave Web API 包。

导出：
* ``create_app(platform, config)`` —— 组装 ``ThreadingHTTPServer``（供 server / 测试使用）
* ``WebApp`` —— 应用对象（路由 + SSE + 静态文件）
* ``serve`` / ``main`` —— 命令行启动入口
"""
from __future__ import annotations

from .app import (
    DEFAULT_CONFIG,
    ApiError,
    AsyncBridge,
    AgentWeaveHTTPServer,
    VERIFY_CHECKERS,
    VERIFY_LAYERS,
    WebApp,
    create_app,
    mask_secret,
)

__all__ = [
    "DEFAULT_CONFIG",
    "ApiError",
    "AsyncBridge",
    "AgentWeaveHTTPServer",
    "VERIFY_CHECKERS",
    "VERIFY_LAYERS",
    "WebApp",
    "create_app",
    "mask_secret",
    "serve",
    "main",
]


def serve(*args: object, **kwargs: object) -> None:
    """延迟导入命令行入口，避免 ``python -m agentweave.api.server`` 重复导入。"""
    from .server import serve as _serve

    _serve(*args, **kwargs)  # type: ignore[arg-type]


def main(*args: object, **kwargs: object) -> int:
    from .server import main as _main

    return _main(*args, **kwargs)  # type: ignore[arg-type]
