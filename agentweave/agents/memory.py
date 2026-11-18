"""Agent 运行级记忆：键值存储 + 相关笔记召回 + 产物登记。

每个 run（一次工作流执行）拥有独立的 :class:`AgentMemory`。它承担三件事：

1. **键值记忆**：``set`` / ``get`` 保存结构化中间结果（例如数据路径、系数）；
2. **笔记与召回**：``note`` 追加文本笔记，``recall`` 用字符二元组 + 英文词
   Jaccard 相似度返回最相关的 k 条，供 Agent 在后续节点复用上文；
3. **产物登记**：``store_artifact`` 记录报告/图表等产物，``artifacts`` 只读访问。

所有方法均线程安全（工作流可在多线程/多协程中并发写同一 run 的记忆）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _bigrams(text: str) -> Counter:
    """字符二元组计数（对中文短文本的相似度足够稳定）。"""
    compact = re.sub(r"\s+", "", text)
    return Counter(compact[i : i + 2] for i in range(max(0, len(compact) - 1)))


def _jaccard(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 1}


class AgentMemory:
    """run 级线程安全记忆（契约见 docs/CONTRACTS.md §3.2）。"""

    def __init__(self, run_id: str = "", max_notes: int = 500) -> None:
        self.run_id = run_id
        self.max_notes = max(1, int(max_notes))
        self._lock = threading.RLock()
        self._kv: dict[str, Any] = {}
        self._notes: list[dict[str, Any]] = []
        self._artifacts: list[dict[str, Any]] = []
        self._seq = 0

    # ------------------------------------------------------------------
    # 键值
    # ------------------------------------------------------------------
    def set(self, key: str, value: Any) -> Any:
        """写入键值记忆并返回写入值。"""
        with self._lock:
            self._kv[str(key)] = value
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """读取键值记忆。"""
        with self._lock:
            return self._kv.get(str(key), default)

    def keys(self) -> list[str]:
        """返回全部键（快照）。"""
        with self._lock:
            return list(self._kv.keys())

    def as_dict(self) -> dict[str, Any]:
        """返回键值记忆的浅拷贝快照。"""
        with self._lock:
            return dict(self._kv)

    # ------------------------------------------------------------------
    # 笔记
    # ------------------------------------------------------------------
    def note(
        self,
        content: Any,
        *,
        key: str = "",
        node_id: str = "",
        agent_id: str = "",
        kind: str = "note",
        meta: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """追加一条笔记；若给出 ``key`` 同时写入键值记忆。

        ``tags`` 供 RuntimeContext 标注来源节点（``memory.note(..., tags=[node_id])``）。
        """
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        with self._lock:
            self._seq += 1
            entry: dict[str, Any] = {
                "seq": self._seq,
                "ts": time.time(),
                "text": text,
                "key": str(key),
                "node_id": node_id,
                "agent_id": agent_id,
                "kind": kind,
                "meta": dict(meta or {}),
                "tags": [str(t) for t in (tags or [])],
            }
            self._notes.append(entry)
            if len(self._notes) > self.max_notes:
                del self._notes[: len(self._notes) - self.max_notes]
            if key:
                self._kv[str(key)] = content
            return dict(entry)

    def notes(self, node_id: str | None = None) -> list[dict[str, Any]]:
        """返回笔记快照，可按节点过滤。"""
        with self._lock:
            if node_id is None:
                return [dict(n) for n in self._notes]
            return [dict(n) for n in self._notes if n["node_id"] == node_id]

    def recall(self, query: str, k: int = 3) -> list[str]:
        """按相关度返回最多 ``k`` 条笔记文本。"""
        query = str(query or "")
        if not query.strip():
            with self._lock:
                return [n["text"] for n in self._notes[-max(1, int(k)) :]]
        q_bi, q_tok = _bigrams(query), _tokens(query)
        scored: list[tuple[float, int, str]] = []
        with self._lock:
            snapshot = list(self._notes)
        for idx, entry in enumerate(snapshot):
            text = entry["text"]
            score = 0.7 * _jaccard(q_bi, _bigrams(text)) + 0.3 * _jaccard(
                Counter(q_tok), Counter(_tokens(text))
            )
            if query and query in text:
                score += 0.2
            if score > 0:
                scored.append((score, idx, text))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [text for _, _, text in scored[: max(1, int(k))]]

    def summary(self, limit: int = 12) -> str:
        """把最近笔记压缩成一段可读摘要（供后续 Agent 参考）。"""
        with self._lock:
            recent = self._notes[-max(1, int(limit)) :]
        if not recent:
            return ""
        lines: list[str] = []
        for entry in recent:
            prefix = f"[{entry['node_id'] or entry['agent_id'] or entry['kind']}]"
            text = entry["text"].replace("\n", " ")
            if len(text) > 160:
                text = text[:160] + "…"
            lines.append(f"{prefix} {text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 产物
    # ------------------------------------------------------------------
    @property
    def artifacts(self) -> list[dict[str, Any]]:
        """已登记产物列表（快照，避免外部修改内部状态）。"""
        with self._lock:
            return [dict(a) for a in self._artifacts]

    def store_artifact(
        self,
        name: str,
        content: Any,
        kind: str = "artifact",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一个产物（名称、类型、大小、内容预览）。"""
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        with self._lock:
            record: dict[str, Any] = {
                "name": str(name),
                "kind": kind,
                "size": len(text),
                "preview": text[:200],
                "ts": time.time(),
                "meta": dict(meta or {}),
            }
            self._artifacts.append(record)
            return dict(record)

    def find_artifact(self, name: str) -> dict[str, Any] | None:
        """按名称查找产物记录。"""
        with self._lock:
            for record in reversed(self._artifacts):
                if record["name"] == name:
                    return dict(record)
        return None

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """导出可 JSON 序列化的完整快照。"""
        with self._lock:
            return {
                "run_id": self.run_id,
                "kv": dict(self._kv),
                "notes": [dict(n) for n in self._notes],
                "artifacts": [dict(a) for a in self._artifacts],
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._notes)


__all__ = ["AgentMemory"]
