"""自循环控制器兼容入口（任务书文件名 controller.py）。

实现位于 :mod:`agentweave.loop.selfloop`，两种 import 路径等价。
"""
from __future__ import annotations

from .selfloop import LoopState, SelfLoopController

__all__ = ["LoopState", "SelfLoopController"]
