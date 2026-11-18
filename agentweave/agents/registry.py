"""Agent 注册表：加载 ``config/agents.json``、版本管理、技能匹配与并发配额。

设计要点
--------
* **单例式**：Web/API 层通过 :func:`get_default_registry` 拿到同一份注册表，
  同时保留可构造独立实例的能力（测试友好）；
* **版本历史**：同一 ``id`` 注册不同 ``version`` 时保留旧版本，``history()`` 可追溯；
* **匹配**：``match()`` 以 skill 模糊匹配为主（字符二元组 Jaccard）、工具覆盖为辅，
  返回 ``(AgentSpec, score)`` 列表，供调度器做“谁最适合这个节点”的决策；
* **并发配额**：``acquire()`` / ``release()`` 用非阻塞「在途计数 + 上限」实现
  Agent 级并发上限（默认 2），拿不到配额时立即返回 False，由调用方决定排队策略。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.types import AgentSpec

#: 项目根目录（agentweave/agents/registry.py -> 上溯两级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: 默认 Agent 配置文件。
DEFAULT_AGENTS_PATH = PROJECT_ROOT / "config" / "agents.json"

_WORD_RE = None  # 预留：如需英文分词可在此接入 re


def _holder_token() -> Any:
    """返回并发配额的持有者标识（当前协程任务；同步上下文返回 None）。"""
    try:
        return asyncio.current_task()
    except RuntimeError:  # pragma: no cover - 无事件循环的同步调用
        return None


def _compact(text: str) -> str:
    return "".join(str(text).split()).lower()


def _bigrams(text: str) -> Counter:
    compact = _compact(text)
    return Counter(compact[i : i + 2] for i in range(max(0, len(compact) - 1)))


def _jaccard(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def _skill_similarity(wanted: str, candidates: Iterable[str]) -> float:
    """单个需求技能与 Agent 技能列表的最佳匹配分。"""
    wanted_c = _compact(wanted)
    if not wanted_c:
        return 0.0
    best = 0.0
    wanted_bi = _bigrams(wanted)
    for skill in candidates:
        skill_c = _compact(skill)
        if not skill_c:
            continue
        if skill_c == wanted_c:
            return 1.0
        if wanted_c in skill_c or skill_c in wanted_c:
            best = max(best, 0.85)
            continue
        best = max(best, _jaccard(wanted_bi, _bigrams(skill_c)))
    return best


class AgentRegistry:
    """Agent 定义注册表（契约见 docs/CONTRACTS.md §3.2）。"""

    def __init__(self, load_fn: Callable[[], Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentSpec] = {}
        self._versions: dict[str, dict[str, AgentSpec]] = {}
        self._load_fn = load_fn
        self._limits: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._holders: dict[str, list[Any]] = {}
        self.load_error: str = ""
        if load_fn is not None:
            self.reload()

    # ------------------------------------------------------------------
    # 注册 / 查询
    # ------------------------------------------------------------------
    def register(self, spec: AgentSpec, *, replace: bool = True) -> AgentSpec:
        """注册（或更新）一个 Agent 定义，返回注册后的 spec。"""
        if not isinstance(spec, AgentSpec):
            spec = AgentSpec.from_dict(dict(spec))
        with self._lock:
            versions = self._versions.setdefault(spec.id, {})
            versions[spec.version] = spec
            if replace or spec.id not in self._agents:
                self._agents[spec.id] = spec
            self._limits.setdefault(spec.id, 2)
            self._inflight.setdefault(spec.id, 0)
            self._holders.setdefault(spec.id, [])
        return spec

    def unregister(self, agent_id: str) -> bool:
        """移除 Agent（含其版本历史与并发配额）。"""
        with self._lock:
            existed = self._agents.pop(agent_id, None) is not None
            self._versions.pop(agent_id, None)
            self._limits.pop(agent_id, None)
            self._inflight.pop(agent_id, None)
            self._holders.pop(agent_id, None)
            return existed

    def get(self, agent_id: str, version: str | None = None) -> AgentSpec | None:
        """按 id 取 Agent；``version`` 指定时取历史版本。"""
        with self._lock:
            if version:
                return self._versions.get(agent_id, {}).get(version)
            return self._agents.get(agent_id)

    def find_role(self, role: str) -> AgentSpec | None:
        """按 role 精确匹配（再退回 id 匹配），用于“角色 → Agent”查找。"""
        role = str(role or "").strip()
        if not role:
            return None
        with self._lock:
            for spec in self._agents.values():
                if spec.role == role and spec.enabled:
                    return spec
            for spec in self._agents.values():
                if spec.id == role and spec.enabled:
                    return spec
        return None

    def list(self, *, only_enabled: bool = False) -> list[AgentSpec]:
        """返回全部 Agent（按 id 排序）。"""
        with self._lock:
            specs = list(self._agents.values())
        if only_enabled:
            specs = [s for s in specs if s.enabled]
        return sorted(specs, key=lambda s: s.id)

    def enabled(self) -> list[AgentSpec]:
        """返回启用的 Agent 列表。"""
        return self.list(only_enabled=True)

    def history(self, agent_id: str = "") -> list[str]:
        """返回版本历史字符串列表（``id@version``）；给定 id 时只返回该 Agent 的版本号。"""
        with self._lock:
            if agent_id:
                return sorted(self._versions.get(agent_id, {}).keys())
            rows: list[str] = []
            for aid in sorted(self._versions):
                for ver in sorted(self._versions[aid]):
                    rows.append(f"{aid}@{ver}")
            return rows

    def match(self, skills: list[str] | None = None, tools: list[str] | None = None) -> list[tuple[AgentSpec, float]]:
        """按技能/工具需求给启用的 Agent 打分，降序返回 ``(spec, score)``。"""
        wanted_skills = [str(s) for s in (skills or []) if str(s).strip()]
        wanted_tools = {str(t).strip().lower() for t in (tools or []) if str(t).strip()}
        results: list[tuple[AgentSpec, float]] = []
        for spec in self.enabled():
            skill_score = 0.0
            if wanted_skills:
                skill_score = sum(_skill_similarity(s, spec.skills) for s in wanted_skills) / len(
                    wanted_skills
                )
            tool_score = 0.0
            if wanted_tools:
                owned = {str(t).lower() for t in spec.tools}
                tool_score = len(wanted_tools & owned) / len(wanted_tools)
            if wanted_skills and wanted_tools:
                score = 0.7 * skill_score + 0.3 * tool_score
            elif wanted_skills:
                score = skill_score
            elif wanted_tools:
                score = tool_score
            else:
                score = 0.0
            results.append((spec, round(score, 4)))
        results.sort(key=lambda item: (-item[1], item[0].id))
        return results

    # ------------------------------------------------------------------
    # 并发配额
    # ------------------------------------------------------------------
    def acquire(self, agent_id: str, max_concurrency: int = 2) -> bool:
        """非阻塞获取 Agent 并发配额；成功返回 True，已满返回 False。

        配额以「持有者集合 + 上限」实现：调用方传入的 ``max_concurrency`` 每次都会
        刷新该 Agent 的并发上限；``release()`` 只归还当前协程自己持有的配额，因此
        ``acquire()`` 失败后即便调用方仍然执行了 ``release()``，也不会误扣其他节点的
        配额。全程不阻塞事件循环，适合在异步编排器中做软限流。
        """
        limit = max(1, int(max_concurrency or 2))
        token = _holder_token()
        with self._lock:
            holders = self._holders.setdefault(agent_id, [])
            self._limits[agent_id] = limit
            if len(holders) >= limit:
                return False
            holders.append(token)
            self._inflight[agent_id] = len(holders)
        return True

    def release(self, agent_id: str) -> None:
        """释放当前协程持有的 Agent 并发配额（未持有时静默忽略）。"""
        token = _holder_token()
        with self._lock:
            holders = self._holders.get(agent_id)
            if not holders or token not in holders:
                return
            holders.remove(token)
            self._inflight[agent_id] = len(holders)

    def inflight(self, agent_id: str) -> int:
        """返回某 Agent 当前在途调用数。"""
        with self._lock:
            return int(self._inflight.get(agent_id, 0))

    def load_report(self) -> list[dict[str, Any]]:
        """返回各 Agent 并发利用率报告（供监控面板展示）。"""
        rows: list[dict[str, Any]] = []
        with self._lock:
            for agent_id in sorted(self._agents):
                inflight = int(self._inflight.get(agent_id, 0))
                limit = int(self._limits.get(agent_id, 2)) or 2
                rows.append(
                    {
                        "agent_id": agent_id,
                        "inflight": inflight,
                        "max": limit,
                        "utilization": round(inflight / limit, 4),
                    }
                )
        return rows

    # ------------------------------------------------------------------
    # 加载 / 持久化
    # ------------------------------------------------------------------
    def reload(self) -> int:
        """重新执行加载函数，返回成功注册的 Agent 数量。"""
        loader = self._load_fn
        if loader is None:
            self.load_error = "未配置加载函数（load_fn=None）"
            return 0
        try:
            raw = loader()
        except Exception as exc:  # noqa: BLE001 - 配置损坏时保持旧注册表可用
            self.load_error = f"{type(exc).__name__}: {exc}"
            return 0
        specs = _coerce_specs(raw)
        with self._lock:
            self._agents.clear()
            self._versions.clear()
            for spec in specs:
                self.register(spec)
        self.load_error = ""
        return len(specs)

    def load(self, path: str | Path) -> int:
        """从指定 JSON 文件加载并替换注册表内容（兼容平台装配的 ``registry.load(path)``）。

        返回成功注册的 Agent 数量；文件损坏时保留旧注册表并记录 ``load_error``。
        """
        target = Path(path)

        def _loader() -> Any:
            return json.loads(target.read_text(encoding="utf-8"))

        self._load_fn = _loader
        return self.reload()

    def save(self, path: str | Path) -> None:
        """把当前注册表（每个 id 的最新版本）写回 JSON 文件。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "agents": [spec.to_dict() for spec in self.list()]}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        """导出全部最新 Agent 定义。"""
        return {"agents": [spec.to_dict() for spec in self.list()]}

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        with self._lock:
            return str(agent_id) in self._agents

    def __iter__(self):
        return iter(self.list())


# ----------------------------------------------------------------------
# 模块级辅助
# ----------------------------------------------------------------------
def _coerce_specs(raw: Any) -> list[AgentSpec]:
    """把加载结果归一化为 AgentSpec 列表。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = raw.get("agents", [])
    else:
        items = raw
    specs: list[AgentSpec] = []
    for item in items or []:
        if isinstance(item, AgentSpec):
            specs.append(item)
        elif isinstance(item, dict) and item.get("id"):
            specs.append(AgentSpec.from_dict(item))
    return specs


def load_agents(path: str | Path) -> AgentRegistry:
    """从 ``config/agents.json`` 读取 ``{"agents": [...]}`` 并返回已注册的 registry。

    registry 的 ``load_fn`` 指向同一路径，因此 ``reload()`` 可热加载修改后的配置。
    """
    target = Path(path)

    def _loader() -> Any:
        return json.loads(target.read_text(encoding="utf-8"))

    registry = AgentRegistry(load_fn=_loader)
    return registry


_DEFAULT_REGISTRY: AgentRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_registry(reload: bool = False) -> AgentRegistry:
    """返回进程级默认注册表（首次调用时加载 ``config/agents.json``）。"""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            if DEFAULT_AGENTS_PATH.exists():
                _DEFAULT_REGISTRY = load_agents(DEFAULT_AGENTS_PATH)
            else:
                _DEFAULT_REGISTRY = AgentRegistry()
                _DEFAULT_REGISTRY.load_error = f"未找到 Agent 配置: {DEFAULT_AGENTS_PATH}"
        elif reload and DEFAULT_AGENTS_PATH.exists():
            _DEFAULT_REGISTRY = load_agents(DEFAULT_AGENTS_PATH)
        return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """清空进程级默认注册表（测试隔离用）。"""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = None


__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_AGENTS_PATH",
    "AgentRegistry",
    "load_agents",
    "get_default_registry",
    "reset_default_registry",
]
