"""工作流 DSL 构建器：用链式调用拼装 :class:`Workflow`。

示例::

    wf = (
        WorkflowBuilder("wf-demo", "演示工作流")
        .var("dataset", "air_quality.csv")
        .node("start", "start", "开始")
        .node("plan", "agent", "规划", agent="planner",
              prompt="为 {{dataset}} 制定分析计划", outputs=["summary"])
        .node("end", "end", "结束")
        .edge("start", "plan")
        .edge("plan", "end")
        .build()
    )

构建器只负责**声明**，不做执行；``build()`` 前会检查节点 id 唯一与边端点存在，
尽早暴露拼装错误。
"""
from __future__ import annotations

from typing import Any

from ..core.types import (
    NodeType,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)


class WorkflowBuilder:
    """工作流构建器（节点/边/变量）。"""

    def __init__(self, id: str, name: str, description: str = "") -> None:
        if not id:
            raise ValueError("工作流 id 不能为空")
        self.id = id
        self.name = name
        self.description = description
        self._variables: dict[str, Any] = {}
        self._nodes: list[WorkflowNode] = []
        self._edges: list[WorkflowEdge] = []
        self._node_ids: set[str] = set()

    # ------------------------------------------------------------------
    # 变量
    # ------------------------------------------------------------------
    def var(self, key: str, value: Any) -> "WorkflowBuilder":
        """声明工作流级变量（可在 prompt 模板中用 ``{{key}}`` 引用）。"""
        self._variables[str(key)] = value
        return self

    # ------------------------------------------------------------------
    # 节点
    # ------------------------------------------------------------------
    def node(self, id: str, type: str, label: str = "", **kwargs: Any) -> "WorkflowBuilder":
        """添加节点；``kwargs`` 与 :class:`WorkflowNode` 字段同名。"""
        node_id = str(id)
        if not node_id:
            raise ValueError("节点 id 不能为空")
        if node_id in self._node_ids:
            raise ValueError(f"节点 id 重复: {node_id}")
        try:
            node_type = NodeType(type)
        except ValueError as exc:
            raise ValueError(f"未知节点类型: {type}") from exc
        allowed = {
            "description",
            "agent",
            "tool",
            "args",
            "prompt",
            "inputs",
            "outputs",
            "condition",
            "loop",
            "workflow",
            "verify",
            "retry",
            "timeout_s",
            "position",
            "config",
        }
        unknown = set(kwargs) - allowed
        if unknown:
            raise TypeError(f"WorkflowNode 不支持的字段: {sorted(unknown)}")
        node = WorkflowNode(
            id=node_id,
            type=node_type.value,
            label=label or node_id,
            **kwargs,
        )
        self._nodes.append(node)
        self._node_ids.add(node_id)
        return self

    # ------------------------------------------------------------------
    # 边
    # ------------------------------------------------------------------
    def edge(
        self,
        source: str,
        target: str,
        condition: str | None = None,
        label: str = "",
        kind: str = "data",
    ) -> "WorkflowBuilder":
        """添加有向边；``condition`` 为真值表达式，空表示默认边。"""
        source_id, target_id = str(source), str(target)
        for endpoint in (source_id, target_id):
            if endpoint not in self._node_ids:
                raise ValueError(f"边引用了不存在的节点: {endpoint}")
        if kind not in {"data", "control", "loop_back"}:
            raise ValueError(f"未知边类型: {kind}")
        self._edges.append(
            WorkflowEdge(
                source=source_id,
                target=target_id,
                condition=condition,
                label=label,
                kind=kind,
            )
        )
        return self

    def chain(self, *node_ids: str, condition: str | None = None) -> "WorkflowBuilder":
        """顺序连接多个节点（便捷方法）。"""
        for source, target in zip(node_ids, node_ids[1:]):
            self.edge(source, target, condition=condition)
        return self

    def loop_back(self, source: str, target: str, label: str = "") -> "WorkflowBuilder":
        """把 ``source`` 指回 ``target`` 标记为循环回边（拓扑排序时忽略）。"""
        return self.edge(source, target, label=label, kind="loop_back")

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> Workflow:
        """生成不可变的 :class:`Workflow` 对象。"""
        covered = {edge.source for edge in self._edges} | {
            edge.target for edge in self._edges
        }
        isolated = [node.id for node in self._nodes if node.id not in covered]
        if len(self._nodes) > 1 and isolated:
            # 允许孤立节点（并行根/单节点分支），但显式提示避免拼装遗漏
            pass
        return Workflow(
            id=self.id,
            name=self.name,
            description=self.description,
            nodes=list(self._nodes),
            edges=list(self._edges),
            variables=dict(self._variables),
        )

    # ------------------------------------------------------------------
    # 调试
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """返回待构建工作流的字典形式（不触发 build）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "variables": dict(self._variables),
            "nodes": [node.to_dict() for node in self._nodes],
            "edges": [edge.to_dict() for edge in self._edges],
        }

    def __len__(self) -> int:
        return len(self._nodes)


__all__ = ["WorkflowBuilder"]
