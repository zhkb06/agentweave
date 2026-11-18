"""工作流仓库：从 ``workflows/*.json`` 加载、保存与校验工作流定义。

设计要点
--------
* 一个 JSON 文件对应一个 :class:`Workflow`，顶层字段直接是
  ``id/name/version/description/nodes/edges/variables/tags``；
* 加载是**容错**的：单个文件损坏只记入 ``errors()``，不会让平台启动失败；
* 保存统一写 ``{dir}/{workflow.id}.json``，便于版本管理与人工检查。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core.types import Workflow, WorkflowEdge, WorkflowNode

#: 项目默认工作流目录（仓库根 / workflows）
DEFAULT_WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


class WorkflowStore:
    """工作流定义仓库（文件系统为唯一事实来源）。"""

    def __init__(self, workflow_dirs: list[str] | None = None) -> None:
        dirs = list(workflow_dirs) if workflow_dirs else [str(DEFAULT_WORKFLOW_DIR)]
        self.workflow_dirs: list[str] = [str(Path(item).expanduser()) for item in dirs]
        self._workflows: dict[str, Workflow] = {}
        self._sources: dict[str, str] = {}
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def load(self) -> list[Workflow]:
        """扫描所有目录并加载 JSON 工作流；返回按文件名排序的工作流列表。"""
        self._workflows = {}
        self._sources = {}
        self._errors = []
        for directory in self.workflow_dirs:
            base = Path(directory)
            files: list[Path] = []
            if base.is_file() and base.suffix.lower() == ".json":
                files = [base]
            elif base.is_dir():
                files = sorted(base.glob("*.json"), key=lambda p: p.name)
            else:
                self._errors.append(f"工作流目录不存在: {directory}")
                continue
            for path in files:
                self._load_file(path)
        return self.all()

    def reload(self) -> list[Workflow]:
        """``load`` 的语义化别名。"""
        return self.load()

    def _load_file(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._errors.append(f"读取失败 {path}: {exc}")
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            self._errors.append(f"JSON 解析失败 {path.name}: {exc}")
            return
        candidates: list[dict[str, Any]]
        if isinstance(data, dict) and "workflow" in data and "id" not in data:
            inner = data["workflow"]
            candidates = [inner] if isinstance(inner, dict) else []
        elif isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        else:
            candidates = []
        if not candidates:
            self._errors.append(f"文件不是合法工作流 JSON: {path.name}")
            return
        for item in candidates:
            try:
                workflow = self._build_workflow(item)
            except (KeyError, TypeError, ValueError) as exc:
                self._errors.append(f"工作流构建失败 {path.name}: {exc}")
                continue
            if not workflow.id:
                self._errors.append(f"工作流缺少 id: {path.name}")
                continue
            if workflow.id in self._workflows:
                self._errors.append(
                    f"工作流 id 重复，后者覆盖前者: {workflow.id} ({path.name})"
                )
            self._workflows[workflow.id] = workflow
            self._sources[workflow.id] = str(path)

    @staticmethod
    def _build_workflow(data: dict[str, Any]) -> Workflow:
        """优先用 ``Workflow.from_dict``；失败时手工装配以兼容旧格式。"""
        if hasattr(Workflow, "from_dict"):
            return Workflow.from_dict(data)  # type: ignore[no-any-return]
        nodes = [
            WorkflowNode.from_dict(item)
            for item in data.get("nodes", [])
            if isinstance(item, dict)
        ]
        edges = [
            WorkflowEdge.from_dict(item)
            for item in data.get("edges", [])
            if isinstance(item, dict)
        ]
        return Workflow(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "1.0")),
            description=str(data.get("description", "")),
            nodes=nodes,
            edges=edges,
            variables=dict(data.get("variables", {}) or {}),
            owner=str(data.get("owner", "")),
            tags=list(data.get("tags", []) or []),
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def all(self) -> list[Workflow]:
        """全部已加载工作流（保持加载顺序）。"""
        return list(self._workflows.values())

    def get(self, workflow_id: str) -> Workflow | None:
        """按 id 取工作流。"""
        return self._workflows.get(str(workflow_id))

    def ids(self) -> list[str]:
        """全部工作流 id。"""
        return list(self._workflows)

    def errors(self) -> list[str]:
        """上次 ``load`` 期间的非致命错误（供平台日志/健康检查）。"""
        return list(self._errors)

    def source_of(self, workflow_id: str) -> str:
        """工作流来源文件路径。"""
        return self._sources.get(str(workflow_id), "")

    def __len__(self) -> int:
        return len(self._workflows)

    def __contains__(self, workflow_id: object) -> bool:
        return str(workflow_id) in self._workflows

    def __iter__(self):
        return iter(self._workflows.values())

    # ------------------------------------------------------------------
    # 写入 / 校验
    # ------------------------------------------------------------------
    def save(self, wf: Workflow) -> str:
        """保存工作流到 ``{dir}/{wf.id}.json``，返回文件路径。"""
        if not wf or not wf.id:
            raise ValueError("工作流必须有 id 才能保存")
        directory = Path(self.workflow_dirs[0])
        if directory.suffix.lower() == ".json":
            directory = directory.parent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{wf.id}.json"
        text = json.dumps(wf.to_dict(), ensure_ascii=False, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
        self._workflows[wf.id] = wf
        self._sources[wf.id] = str(path)
        return str(path)

    def delete(self, workflow_id: str) -> bool:
        """从内存移除；磁盘文件仅在来源位于仓库目录内时删除。"""
        workflow_id = str(workflow_id)
        source = self._sources.pop(workflow_id, "")
        removed = self._workflows.pop(workflow_id, None) is not None
        if removed and source:
            try:
                if any(
                    os.path.commonpath(
                        [os.path.abspath(source), os.path.abspath(folder)]
                    )
                    == os.path.abspath(folder)
                    for folder in self.workflow_dirs
                    if os.path.isdir(folder)
                ):
                    os.remove(source)
            except (OSError, ValueError):
                pass
        return removed

    def validate(self, wf: Workflow) -> list[str]:
        """校验工作流结构，返回中文问题列表（空列表表示合法）。"""
        problems: list[str] = []
        if wf is None:
            return ["工作流为空"]
        if not getattr(wf, "id", ""):
            problems.append("缺少工作流 id")
        if not getattr(wf, "name", ""):
            problems.append("缺少工作流名称")
        nodes = list(getattr(wf, "nodes", []) or [])
        if not nodes:
            problems.append("工作流没有任何节点")
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                problems.append(f"节点 id 重复: {node.id}")
            seen.add(node.id)
        for edge in getattr(wf, "edges", []) or []:
            if edge.source not in seen:
                problems.append(f"边引用了不存在的源节点: {edge.source}")
            if edge.target not in seen:
                problems.append(f"边引用了不存在的目标节点: {edge.target}")
        try:
            wf.topological_order()
        except (ValueError, KeyError) as exc:
            problems.append(f"拓扑结构非法: {exc}")
        for node in nodes:
            if getattr(node, "type", "") == "agent" and not getattr(
                node, "agent", ""
            ):
                problems.append(f"Agent 节点 {node.id} 未指定 agent")
            if getattr(node, "type", "") == "tool" and not getattr(
                node, "tool", ""
            ):
                problems.append(f"工具节点 {node.id} 未指定 tool")
        try:
            problems.extend(wf.validate())
        except Exception as exc:  # types.validate 已足够健壮，此处仅兜底
            problems.append(f"内置校验异常: {exc}")
        # 去重但保持顺序
        return list(dict.fromkeys(problems))


__all__ = ["DEFAULT_WORKFLOW_DIR", "WorkflowStore"]
