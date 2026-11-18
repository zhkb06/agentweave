"""平台门面：把各子系统装配成可被 Web API / CLI / 测试调用的一个对象。

装配清单
--------
* ``EventBus``：运行事件总线（SSE 实时推送 + 历史回放）；
* ``RunStore``：运行状态仓库（线程安全，供 API 读取）；
* ``Workflow`` 字典：从 ``workflows/*.json`` 载入，支持保存后热更新；
* ``AgentRegistry`` / ``ToolRegistry``：来自 agents-tools 模块；
* ``LLM`` 提供方 + ``ModelRouter``：默认离线 Mock，可热切换 OpenAI 兼容接口；
* 后台 asyncio 事件循环：运行在独立线程，执行引擎的所有协程都在其中调度。

设计要点
--------
1. **同步门面、异步内核**：HTTP 线程调用 ``start_run()`` 立即拿到 run_id，
   真正的执行由 ``Orchestrator`` 提交到平台事件循环，互不阻塞；
2. **演示可复现**：默认 ``mock`` 提供方输出确定性结果，断网也能完整演示；
3. **依赖防御**：各子系统通过 ``_resolve()`` 按候选模块名导入，便于模块并行开发；
4. **真值只在此注入**：数据集真值不写进工作流 JSON，由平台在运行前注入，
   避免 Agent 通过读取工作流定义“偷看答案”。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import importlib
import json
import logging
import shutil
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..core.events import EventBus, Subscription
from ..core.types import (
    AgentSpec,
    EventType,
    RunRequest,
    RunStatus,
    Workflow,
    new_id,
)
from .state import RunStore, RunState

log = logging.getLogger("agentweave.platform")

REPO_ROOT = Path(__file__).resolve().parents[2]

TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.REJECTED.value,
}

DEFAULT_PLATFORM_CONFIG: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8765},
    "llm": {
        "provider": "mock",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "AGENTWEAVE_API_KEY",
        "timeout_s": 60,
    },
    "router": {"planner": "smart", "statistician": "smart", "verifier": "smart"},
    "scheduler": {"policy": "adaptive", "max_concurrent_nodes": 4, "agent_max_concurrency": 2},
    "runtime": {"auto_approve": True, "max_steps": 500, "max_runs": 200},
    "mcp_servers": [],
}


# ---------------------------------------------------------------------------
# 依赖解析：允许实现模块名/导出名有少量差异
# ---------------------------------------------------------------------------
def _resolve(attr: str, modules: tuple[str, ...]) -> Any:
    """按候选模块顺序查找属性，找不到时抛出带上下文的 ImportError。"""
    errors: list[str] = []
    for module_name in modules:
        try:
            module = importlib.import_module(module_name, package="agentweave")
        except ImportError as exc:  # 模块尚不存在（并行开发阶段）
            errors.append(f"{module_name}: {exc}")
            continue
        if hasattr(module, attr):
            return getattr(module, attr)
        errors.append(f"{module_name}: 缺少 {attr}")
    raise ImportError(f"无法解析 {attr}（候选 {modules}）：" + "；".join(errors))


def _try_resolve(attr: str, modules: tuple[str, ...]) -> Any:
    try:
        return _resolve(attr, modules)
    except ImportError:
        return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


ENV_FILE_PATH = REPO_ROOT / ".env"

# 环境变量 → ``llm`` 段覆盖（API Key 只经环境变量读取，永不写入配置文件）
_LLM_ENV_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("AGENTWEAVE_PROVIDER", "provider"),
    ("AGENTWEAVE_BASE_URL", "base_url"),
    ("AGENTWEAVE_MODEL", "model"),
)


def load_runtime_env(env_file: str | Path | None = None) -> list[str]:
    """加载 ``.env`` 到进程环境变量（真实环境变量优先），返回写入的键名。

    默认读取仓库根目录 ``.env``；文件不存在时静默跳过，便于离线演示。
    """
    from ..core.envfile import load_env_file

    return load_env_file(ENV_FILE_PATH if env_file is None else env_file)


def apply_llm_env_overrides(raw: dict[str, Any]) -> dict[str, str]:
    """把 ``AGENTWEAVE_PROVIDER/BASE_URL/MODEL`` 环境变量合并进配置 ``llm`` 段。

    返回实际生效的 ``{环境变量名: 值}``；空值忽略（保留配置文件原值）。
    """
    llm_cfg = raw.get("llm")
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
        raw["llm"] = llm_cfg
    applied: dict[str, str] = {}
    for env_name, key in _LLM_ENV_OVERRIDES:
        value = str(os.environ.get(env_name) or "").strip()
        if not value:
            continue
        llm_cfg[key] = value
        applied[env_name] = value
    return applied


def load_platform_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 ``config/platform.json`` 并与内置默认值合并。

    读取前先加载仓库根目录的 ``.env``（若存在），随后 ``AGENTWEAVE_PROVIDER`` /
    ``AGENTWEAVE_BASE_URL`` / ``AGENTWEAVE_MODEL`` 覆盖 ``llm`` 段；API Key 始终
    只从环境变量（默认 ``AGENTWEAVE_API_KEY``）读取，不写入配置文件。
    """
    load_runtime_env()
    target = Path(path) if path else REPO_ROOT / "config" / "platform.json"
    raw: dict[str, Any] = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError) as exc:
            log.warning("读取平台配置失败（使用默认值）：%s", exc)
    applied = apply_llm_env_overrides(raw)
    if applied:
        log.info("已按环境变量覆盖 LLM 配置：%s", ", ".join(sorted(applied)))
    return _deep_merge(DEFAULT_PLATFORM_CONFIG, raw)


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
class Platform:
    """平台门面。所有 ``_api``/公共方法均为同步，内部把协程提交到自建循环。"""

    def __init__(self, config: dict[str, Any] | None = None, root: str | Path | None = None) -> None:
        self.root: Path = Path(root).resolve() if root else REPO_ROOT
        payload = config or {}
        if isinstance(payload.get("platform_config"), dict):
            payload = payload["platform_config"]
        self.config: dict[str, Any] = _deep_merge(DEFAULT_PLATFORM_CONFIG, payload)

        # API Key 只存在本进程内存中（用户每次进入 Web 时重新输入），
        # 绝不写入 config/ 或日志；环境变量 AGENTWEAVE_API_KEY 仅作为无界面运行时的可选项。
        self._session_api_key: str = ""
        self.bus = EventBus()
        runtime_cfg = self.config.get("runtime") or {}
        self.store = RunStore(max_runs=int(runtime_cfg.get("max_runs") or 200))
        # 「并发检查 + 创建 run」必须原子：否则多线程同时 start_run 时
        # 会各自读到未超限的计数，把上限穿透（实测 4 上限放进来 5 个）。
        self._admission_lock = threading.Lock()

        self.workflows: dict[str, Workflow] = {}
        self._reload_workflows()

        self.agents = self._build_agents()
        self.tools = self._build_tools()
        self.llm = self._build_llm()
        self.scheduler = self._build_scheduler()

        self.loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._started = False
        self._tasks: dict[str, Any] = {}
        self._orchestrator: Any = None
        self.started_at: float = time.time()

        # 演示数据真值（运行前注入到工作流验证节点）
        self._inject_ground_truth()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> "Platform":
        """启动后台事件循环（幂等）。"""
        with self._start_lock:
            if self._started:
                return self
            self.loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._run_loop, name="agentweave-loop", daemon=True
            )
            self._loop_thread.start()
            self._started = True
            log.info("平台事件循环已启动（版本 %s）", __version__)
            self._cleanup_runs()
        return self

    def _run_loop(self) -> None:
        loop = self.loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:  # pragma: no cover - 进程退出路径
            try:
                loop.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        """停止事件循环并释放资源（幂等，可安全重复调用）。"""
        with self._start_lock:
            if not self._started:
                return
            self._started = False
            loop = self.loop
            self.loop = None
        for run_id, future in list(self._tasks.items()):
            try:
                future.cancel()
            except Exception:
                pass
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
        self._loop_thread = None
        log.info("平台已关闭")

    @property
    def started(self) -> bool:
        return self._started and self.loop is not None and self.loop.is_running()

    def ensure_started(self) -> None:
        if not self.started:
            self.start()

    def _submit(self, coro: Any, label: str = "") -> Any:
        """把协程提交到平台事件循环，返回 concurrent.futures.Future。"""
        self.ensure_started()
        assert self.loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if label:
            self._tasks[label] = future

            def _cleanup(_fut: Any) -> None:
                self._tasks.pop(label, None)

            future.add_done_callback(_cleanup)
        return future

    # ------------------------------------------------------------------
    # 子系统装配
    # ------------------------------------------------------------------
    def _build_agents(self) -> Any:
        load_agents = _try_resolve("load_agents", ("agentweave.agents.registry", "agentweave.agents"))
        spec_path = self.root / "config" / "agents.json"
        if load_agents is not None:
            try:
                registry = load_agents(str(spec_path))
                if registry is not None:
                    return registry
            except Exception as exc:  # pragma: no cover - 装配降级
                log.warning("Agent 注册表加载失败：%s", exc)
        AgentRegistry = _try_resolve("AgentRegistry", ("agentweave.agents.registry", "agentweave.agents"))
        if AgentRegistry is not None:
            try:
                registry = AgentRegistry()
                registry.load(str(spec_path))  # type: ignore[attr-defined]
                return registry
            except Exception as exc:  # pragma: no cover
                log.warning("AgentRegistry 初始化失败：%s", exc)
        return _SimpleAgentRegistry.from_file(spec_path)

    def _build_tools(self) -> Any:
        factory = _try_resolve(
            "build_tool_registry", ("agentweave.tools.builtin", "agentweave.tools.registry")
        )
        if factory is not None:
            try:
                return factory(str(self.root), bus=self.bus)
            except TypeError:
                try:
                    return factory(str(self.root))
                except Exception as exc:  # pragma: no cover
                    log.warning("工具注册表构造失败：%s", exc)
            except Exception as exc:  # pragma: no cover
                log.warning("工具注册表构造失败：%s", exc)
        ToolRegistry = _try_resolve("ToolRegistry", ("agentweave.tools.registry", "agentweave.tools"))
        return ToolRegistry() if ToolRegistry is not None else None

    def _build_llm(self) -> Any:
        router_cls = _try_resolve("ModelRouter", ("agentweave.llm.router", "agentweave.llm"))
        llm_cfg = dict(self.config.get("llm") or {})
        router_cfg = self.config.get("router") or {}
        if isinstance(router_cfg, dict) and router_cfg:
            llm_cfg.setdefault("routing", dict(router_cfg))
        if router_cls is not None:
            for attempt in ({"config": llm_cfg}, {"config": llm_cfg, "providers": None}):
                try:
                    router = router_cls(**attempt)
                    # 记录路由配置，供 llm_status 展示
                    self._router_cfg = router_cfg
                    if str(llm_cfg.get("provider") or "mock").strip().lower() in ("mock", "", "offline"):
                        self._install_scenario_provider(router)
                    return router
                except TypeError:
                    continue
                except Exception as exc:  # pragma: no cover
                    log.warning("ModelRouter 初始化失败：%s", exc)
                    break
        return self._make_provider(llm_cfg)

    def _install_scenario_provider(self, router: Any) -> None:
        """默认 Mock 模式下安装多场景包装器；失败不影响基础演示。"""
        try:
            from ..llm.scenario_mock import ScenarioMockProvider
        except Exception as exc:  # pragma: no cover
            log.warning("多场景 Mock 不可用：%s", exc)
            return
        install = getattr(router, "install_provider", None)
        if not callable(install):
            return
        for tier in ("fast", "smart"):
            try:
                install(tier, ScenarioMockProvider())
            except Exception as exc:  # pragma: no cover
                log.debug("安装多场景 Mock（%s）失败：%s", tier, exc)

    def _make_provider(self, llm_cfg: dict[str, Any]) -> Any:
        provider_name = str(llm_cfg.get("provider") or "mock").strip().lower()
        if provider_name in ("mock", "", "offline"):
            MockProvider = _try_resolve(
                "MockProvider", ("agentweave.llm.mock", "agentweave.llm")
            )
            if MockProvider is None:
                return _NullProvider()
            return MockProvider() if self._can_call(MockProvider) else MockProvider

        OpenAIProvider = _try_resolve(
            "OpenAICompatProvider",
            ("agentweave.llm.openai_compat", "agentweave.llm.provider", "agentweave.llm"),
        )
        if OpenAIProvider is None:
            log.warning("未找到 OpenAICompatProvider，回退到 Mock")
            return self._make_provider({"provider": "mock"})
        api_key_env = str(llm_cfg.get("api_key_env") or "AGENTWEAVE_API_KEY")
        api_key = (
            os.environ.get(api_key_env)
            or self._session_api_key
            or ""
        )
        llm_cfg = dict(llm_cfg)
        llm_cfg["api_key"] = api_key
        kwargs = {
            "api_key": api_key,
            "base_url": llm_cfg.get("base_url"),
            "model": llm_cfg.get("model"),
            "timeout_s": float(llm_cfg.get("timeout_s") or 60),
        }
        for attempt in (
            kwargs,
            {k: v for k, v in kwargs.items() if v},
            {},
        ):
            try:
                return OpenAIProvider(**attempt)
            except TypeError:
                continue
            except Exception as exc:  # pragma: no cover
                log.warning("OpenAICompatProvider 初始化失败：%s", exc)
                break
        return _NullProvider()

    @staticmethod
    def _can_call(obj: Any) -> bool:
        return callable(obj) and not isinstance(obj, type)

    def _build_scheduler(self) -> Any:
        scheduler_cls = _try_resolve(
            "Scheduler", ("agentweave.scheduler.engine", "agentweave.scheduler")
        )
        if scheduler_cls is None:
            return None
        sched_cfg = dict(self.config.get("scheduler") or {})
        policy = str(sched_cfg.get("policy") or "adaptive")
        for kwargs in (
            {"policy": policy, "max_concurrent_nodes": int(sched_cfg.get("max_concurrent_nodes") or 4)},
            {"policy": policy},
            {},
        ):
            try:
                return scheduler_cls(**kwargs)
            except TypeError:
                continue
            except Exception as exc:  # pragma: no cover
                log.warning("调度器初始化失败：%s", exc)
                break
        return None

    def make_verifier(self, ctx: Any) -> Any:
        """为一次运行构造验证流水线（上下文延迟绑定到本次运行）。"""
        pipeline_cls = _try_resolve(
            "VerificationPipeline",
            ("agentweave.verify.verifier", "agentweave.verify.pipeline", "agentweave.verify"),
        )
        if pipeline_cls is None:
            return _NullVerifier()
        for kwargs in (
            {"ctx_provider": lambda: ctx},
            {"context_provider": lambda: ctx},
            {},
        ):
            try:
                return pipeline_cls(**kwargs)
            except TypeError:
                continue
            except Exception as exc:  # pragma: no cover
                log.warning("验证流水线初始化失败：%s", exc)
                break
        return _NullVerifier()

    # ------------------------------------------------------------------
    # 工作流
    # ------------------------------------------------------------------
    def _workflow_dir(self) -> Path:
        return self.root / "workflows"

    def _reload_workflows(self) -> None:
        """从 ``workflows/*.json`` 与 ``scenarios/*/workflow.json`` 载入全部工作流。"""
        directories: list[Path] = [self._workflow_dir()]
        scenarios_dir = self.root / "scenarios"
        if scenarios_dir.is_dir():
            directories.extend(sorted(p for p in scenarios_dir.iterdir() if p.is_dir()))
        loaded: dict[str, Workflow] = {}
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.name not in ("workflow.json", "scenario.json", "truth.json") and directory != self._workflow_dir():
                    continue
                if path.name in ("scenario.json", "truth.json"):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    wf = Workflow.from_dict(data)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    log.warning("工作流 %s 载入失败：%s", path.name, exc)
                    continue
                errors = wf.validate()
                if errors:
                    log.warning("工作流 %s 校验未通过：%s", wf.id, "；".join(errors))
                loaded[wf.id] = wf
        self.workflows = loaded

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        wf = self._resolve_workflow(workflow_id, "")
        return wf.to_dict() if wf else None

    def list_workflows(self) -> list[dict[str, Any]]:
        return [wf.to_dict() for wf in self.workflows.values()]

    def save_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存（新建/覆盖）工作流到 ``workflows/{id}.json``，并热更新内存。"""
        wf = Workflow.from_dict(payload)
        errors = wf.validate()
        if errors:
            raise ValueError("工作流校验失败：" + "；".join(errors))
        self._workflow_dir().mkdir(parents=True, exist_ok=True)
        path = self._workflow_dir() / f"{wf.id}.json"
        path.write_text(
            json.dumps(wf.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._reload_workflows()
        self._inject_ground_truth()
        return wf.to_dict()

    def _resolve_workflow(self, workflow_id: str, scenario: str) -> Workflow | None:
        """解析工作流；**显式指定的 workflow_id 不会回退到 scenario**。

        回退只在「没有显式 workflow_id、只给了 scenario」时发生——否则用户把
        工作流名写错（如 ``wf-air-qualit``）时会静默改跑另一个工作流，返回 200
        却与意图不符。找不到时返回 ``None``，由 ``start_run`` 抛错（API 映射 404）。
        """
        if workflow_id:
            candidates = [workflow_id]
            if not workflow_id.startswith("wf-"):
                candidates.append(f"wf-{workflow_id}")
        elif scenario:
            candidates = [scenario, f"wf-{scenario}"]
        else:
            return None
        for candidate in candidates:
            wf = self.workflows.get(candidate)
            if wf is not None:
                return wf
        # 内存未命中时尝试从磁盘补载
        self._reload_workflows()
        for candidate in candidates:
            wf = self.workflows.get(candidate)
            if wf is not None:
                return wf
        return None

    # ------------------------------------------------------------------
    # 真值注入
    # ------------------------------------------------------------------
    def _truth_path(self) -> Path:
        return self.root / "examples" / "datasets" / "air_quality_truth.json"

    def _load_truth(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # pragma: no cover
            log.warning("真值文件 %s 解析失败：%s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def _workflow_truth(self, wf: Workflow) -> dict[str, Any] | None:
        """按工作流变量 ``scenario_id`` 读取对应场景真值；否则用默认演示真值。"""
        variables = getattr(wf, "variables", None) or {}
        scenario_id = str(variables.get("scenario_id") or "").strip()
        if scenario_id:
            truth = self._load_truth(self.root / "scenarios" / scenario_id / "truth.json")
            if truth:
                return truth
        return self._load_truth(self._truth_path())

    def _inject_ground_truth(self) -> None:
        """把隐藏真值注入演示工作流的验证节点（不落盘到工作流 JSON）。"""
        default_truth = self._load_truth(self._truth_path())
        injected = 0
        for wf in self.workflows.values():
            truth = self._workflow_truth(wf) or default_truth
            if not isinstance(truth, dict):
                continue
            # 只注入「可被申报指标直接比对」的真值；tolerance/n/r2_min 走 verify 配置与
            # statistical 检查器的 thresholds，避免 numeric_ground_truth 把配置键当成缺失指标。
            ground_truth = {"coefficients": dict(truth.get("coefficients") or {})}
            for node in wf.nodes:
                if node.verify is None:
                    continue
                checks = set(node.verify.checks or [])
                if checks & {"numeric_ground_truth", "code_rerun", "statistical", "cross_agent"}:
                    node.verify.ground_truth = ground_truth
                    injected += 1
        if injected:
            log.debug("已注入 %d 个验证节点的隐藏真值", injected)

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            from .orchestrator import Orchestrator

            self._orchestrator = Orchestrator(self)
        return self._orchestrator

    def start_run(self, request: RunRequest) -> str:
        """创建运行并异步执行，立即返回 run_id。"""
        if not isinstance(request, RunRequest):
            request = RunRequest(**dict(request))  # type: ignore[arg-type]
        wf = self._resolve_workflow(request.workflow_id, request.scenario)
        if wf is None:
            raise ValueError(f"工作流不存在：{request.workflow_id}")
        policy = self._normalize_policy(request.policy)
        if policy != request.policy:
            request = dataclasses.replace(request, policy=policy)
        max_concurrent = int((self.config.get("runtime") or {}).get("max_concurrent_runs") or 0)
        with self._admission_lock:
            if max_concurrent > 0:
                active = sum(
                    1
                    for item in self.store.list(200)
                    if str(item.get("status")) in {"pending", "running", "paused"}
                )
                if active >= max_concurrent:
                    raise RuntimeError(
                        f"并发运行数已达上限（{max_concurrent}），请等待当前运行结束后重试"
                    )
            self._inject_ground_truth()
            run = self.store.create(request, wf)
        run.artifacts_dir = str(self.root / "data" / "runs" / run.run_id)
        os.makedirs(run.artifacts_dir, exist_ok=True)
        self.bus.emit(
            EventType.RUN_CREATED,
            run_id=run.run_id,
            payload={
                "workflow_id": wf.id,
                "workflow_name": wf.name,
                "policy": request.policy,
                "label": request.label,
                "scenario": request.scenario,
            },
        )
        future = self._submit(self.orchestrator.execute(run, wf, request), label=run.run_id)
        future.add_done_callback(lambda fut, rid=run.run_id: self._on_run_done(rid, fut))
        log.info("运行 %s 已提交（工作流 %s，策略 %s）", run.run_id, wf.id, request.policy)
        return run.run_id

    @staticmethod
    def _normalize_policy(policy: str) -> str:
        """归一化调度策略名；未指定时回落默认策略，未知策略抛 ``ValueError``。

        调度器模块按候选路径解析，解析失败时原样返回，保持平台可降级启动。
        """
        text = str(policy or "").strip()
        if not text:
            return "capability"
        try:
            from ..scheduler.engine import normalize_policy
        except ImportError:  # pragma: no cover - 调度器缺失时降级
            return text
        return normalize_policy(text)

    def _on_run_done(self, run_id: str, future: Any) -> None:
        # 先看运行状态：编排器正常收尾时（RUNNING→SUCCEEDED）未来可能恰好在
        # shutdown() 里被 cancel，这属于正常收尾竞争，不应记成异常。
        finished = self.store.get(run_id)
        if finished is None or finished.status in TERMINAL_STATUSES:
            return
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            run = self.store.get(run_id)
            if run is not None and run.status not in TERMINAL_STATUSES:
                self.store.finish(run, RunStatus.CANCELLED.value, "运行已取消")
                self.bus.emit(
                    EventType.RUN_FINISHED,
                    run_id=run_id,
                    payload={"status": RunStatus.CANCELLED.value},
                )
        except Exception as exc:  # pragma: no cover - 兜底，避免静默丢异常
            log.exception("运行 %s 执行异常", run_id)
            run = self.store.get(run_id)
            if run is not None and run.status not in TERMINAL_STATUSES:
                self.store.finish(run, RunStatus.FAILED.value, f"平台异常：{exc}")
                self.bus.emit(
                    EventType.RUN_FINISHED,
                    run_id=run_id,
                    payload={"status": RunStatus.FAILED.value, "error": str(exc)},
                    level="error",
                )

    def wait_run(self, run_id: str, timeout: float = 120.0) -> dict[str, Any] | None:
        """阻塞等待运行结束（CLI / 测试使用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.store.get(run_id)
            if run is None:
                return None
            if run.status in TERMINAL_STATUSES:
                return run.to_dict()
            time.sleep(0.05)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get(run_id)
        if run is not None:
            return run.to_dict()
        # 进程重启后内存态已丢失：回落到磁盘 summary.json（只读摘要）
        return self._read_summary(run_id)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, int(limit or 50))
        runs = self.store.list(limit)
        if len(runs) >= limit:
            return runs
        seen = {str(item.get("run_id") or "") for item in runs}
        merged = list(runs)
        for item in self._disk_summaries(limit):
            run_id = str(item.get("run_id") or "")
            if run_id and run_id not in seen:
                merged.append(item)
                seen.add(run_id)
        merged.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
        return merged[:limit]

    def _runs_root(self) -> Path:
        return self.root / "data" / "runs"

    def _read_summary(self, run_id: str) -> dict[str, Any] | None:
        path = self._runs_root() / run_id / "summary.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _disk_summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        root = self._runs_root()
        if not root.is_dir():
            return []
        try:
            directories = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for directory in directories:
            if len(items) >= limit or not directory.is_dir():
                continue
            data = self._read_summary(directory.name)
            if data:
                items.append(data)
        return items

    def _cleanup_runs(self) -> None:
        try:
            keep = int((self.config.get("runtime") or {}).get("run_history_keep") or 50)
        except (TypeError, ValueError):
            keep = 50
        root = self._runs_root()
        if keep <= 0 or not root.is_dir():
            return
        try:
            directories = sorted(
                (item for item in root.iterdir() if item.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        removed = 0
        for directory in directories[keep:]:
            try:
                shutil.rmtree(directory)
                removed += 1
            except OSError:
                continue
        if removed:
            log.info("运行历史清理：保留 %s 个，删除 %s 个", keep, removed)


    def cancel_run(self, run_id: str) -> bool:
        run = self.store.get(run_id)
        if run is None:
            return False
        if run.status in TERMINAL_STATUSES:
            return False
        run.cancel()
        self.bus.emit(
            EventType.RUN_CANCEL_REQUESTED,
            run_id=run_id,
            payload={"run_id": run_id},
            level="warning",
        )
        return True

    # ------------------------------------------------------------------
    # 事件 / 验证 / 报告 / 产物
    # ------------------------------------------------------------------
    def get_events(self, run_id: str, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        events = self.bus.history(run_id, int(since_seq or 0))
        events = [event.to_dict() for event in events]
        limit = max(1, int(limit or 500))
        return events[-limit:]

    def subscribe(self, run_id: str) -> Subscription:
        return self.bus.subscribe(run_id)

    def event_stats(self) -> dict[str, Any]:
        return self.bus.stats()

    def stats_timeseries(self) -> list[dict[str, Any]]:
        runs = self.store.list(100)
        series: list[dict[str, Any]] = []
        for run in sorted(runs, key=lambda item: item.get("created_at") or 0):
            metrics = run.get("metrics") or {}
            series.append(
                {
                    "ts": run.get("created_at") or run.get("started_at") or 0,
                    "run_id": run.get("run_id"),
                    "workflow_id": run.get("workflow_id"),
                    "status": run.get("status"),
                    "success": run.get("status") == RunStatus.SUCCEEDED.value,
                    "duration_s": metrics.get("duration_s", 0),
                    "tokens": metrics.get("tokens_total", 0),
                    "confidence": metrics.get("avg_confidence", 0),
                }
            )
        return series

    def get_verification(self, run_id: str) -> list[dict[str, Any]]:
        run = self.store.get(run_id)
        return list(run.verification) if run is not None else []

    def get_loop_status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        return dict(run.self_loop) if run is not None else {}

    def get_report(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get(run_id)
        if run is None:
            return None
        report_path = Path(run.artifacts_dir or "") / "report.md"
        markdown = ""
        if run.artifacts_dir and report_path.is_file():
            try:
                markdown = report_path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover
                markdown = ""
        if not markdown:
            markdown = self._fallback_report(run)
        return {
            "run_id": run_id,
            "title": f"{run.workflow_name or run.workflow_id} · 运行报告",
            "markdown": markdown,
            "path": str(report_path) if run.artifacts_dir and report_path.is_file() else "",
            "status": run.status,
            "metrics": run.metrics(),
            "artifacts": list(run.artifacts),
        }

    def _fallback_report(self, run: RunState) -> str:
        lines = [f"# {run.workflow_name or run.workflow_id}", f"- 运行 ID：{run.run_id}", f"- 状态：{run.status}"]
        for node_id, state in run.node_states.items():
            lines.append(f"- `{node_id}`：{state.status}（置信度 {state.confidence:.2f}）")
        return "\n".join(lines)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        run = self.store.get(run_id)
        return list(run.artifacts) if run is not None else []

    def artifact_path(self, run_id: str, name: str) -> str:
        """返回某运行产物的绝对路径（不做存在性判断，由调用方校验）。

        内存态运行直接用 ``run.artifacts_dir``；进程重启后回落到
        ``data/runs/<run_id>/``。``name`` 由 API 层做穿越校验。
        """
        run = self.store.get(run_id)
        directory = (
            Path(run.artifacts_dir)
            if run is not None and run.artifacts_dir
            else self._runs_root() / run_id
        )
        return str(directory / name)

    # ------------------------------------------------------------------
    # Agent / 工具 / 调度 / LLM 配置
    # ------------------------------------------------------------------
    def _agent_dicts(self) -> list[dict[str, Any]]:
        registry = self.agents
        for method_name in ("list", "all", "values", "specs"):
            method = getattr(registry, method_name, None)
            if callable(method):
                try:
                    items = method()
                except TypeError:
                    continue
                if isinstance(items, dict):
                    items = list(items.values())
                return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in items]
        if isinstance(registry, dict):
            return [spec.to_dict() if hasattr(spec, "to_dict") else dict(spec) for spec in registry.values()]
        return []

    def list_agents(self) -> dict[str, Any]:
        load: list[dict[str, Any]] = []
        describe = getattr(self.agents, "load", None)
        if callable(describe):
            try:
                raw = describe()
                if isinstance(raw, dict):
                    load = [{"agent_id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
                elif isinstance(raw, list):
                    load = [item for item in raw if isinstance(item, dict)]
            except Exception:
                load = []
        return {"agents": self._agent_dicts(), "load": load}

    def save_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = AgentSpec.from_dict(payload)
        registry = self.agents
        for name in ("register", "upsert", "add", "save"):
            method = getattr(registry, name, None)
            if callable(method):
                try:
                    method(spec) if name != "upsert" else method(spec.to_dict())
                    return {"agent": spec.to_dict()}
                except TypeError:
                    method(spec.to_dict())
                    return {"agent": spec.to_dict()}
        raise ValueError("当前 Agent 注册表不支持保存")

    register_agent = save_agent

    def list_tools(self) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        registry = self.tools
        for name in ("list", "all", "specs"):
            method = getattr(registry, name, None)
            if callable(method):
                try:
                    raw = method()
                except TypeError:
                    continue
                if isinstance(raw, dict):
                    raw = list(raw.values())
                tools = [
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in raw
                ]
                break
        return {"tools": tools, "mcp_servers": self.list_mcp_servers()}

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        servers = self.config.get("mcp_servers") or []
        return [s if isinstance(s, dict) else {"name": str(s)} for s in servers]

    mcp_servers = list_mcp_servers

    def add_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        servers = list(self.config.get("mcp_servers") or [])
        servers.append(dict(payload))
        self.config["mcp_servers"] = servers
        self._persist_config()
        client_cls = _try_resolve("MCPClient", ("agentweave.tools.mcp_client", "agentweave.tools"))
        if client_cls is not None:
            try:
                self.tools.register_mcp(payload)  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - MCP 可选
                log.warning("注册 MCP Server 失败：%s", exc)
        return payload

    def scheduler_status(self) -> dict[str, Any]:
        scheduler = self.scheduler
        if scheduler is None:
            return {"policy": (self.config.get("scheduler") or {}).get("policy", "adaptive"), "available": False}
        describe = getattr(scheduler, "describe", None)
        if callable(describe):
            try:
                data = describe()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"policy": getattr(scheduler, "policy", "adaptive"), "available": True}

    def verify_strategies(self) -> dict[str, Any]:
        return {
            "checkers": [
                {"name": "schema", "label": "结构契约", "layer": "structure"},
                {"name": "non_empty", "label": "非空检查", "layer": "structure"},
                {"name": "numeric_ground_truth", "label": "真值比对", "layer": "numeric"},
                {"name": "code_rerun", "label": "代码复算", "layer": "numeric"},
                {"name": "statistical", "label": "统计阈值", "layer": "statistical"},
                {"name": "cross_agent", "label": "交叉复核", "layer": "cross_agent"},
            ],
            "layers": [
                {"name": "structure", "label": "结构层"},
                {"name": "numeric", "label": "数值层"},
                {"name": "statistical", "label": "统计层"},
                {"name": "cross_agent", "label": "交叉层"},
            ],
        }

    def llm_status(self) -> dict[str, Any]:
        llm_cfg = self.config.get("llm") or {}
        api_key_env = str(llm_cfg.get("api_key_env") or "AGENTWEAVE_API_KEY")
        router = self.llm
        status: dict[str, Any] = {}
        describe = getattr(router, "status", None)
        if callable(describe):
            try:
                raw = describe()
                if isinstance(raw, dict):
                    status.update(raw)
            except Exception:
                pass
        provider_name = str(llm_cfg.get("provider") or status.get("provider") or "mock")
        status.update(
            {
                "provider": provider_name,
                "model": llm_cfg.get("model") or status.get("model") or "",
                "base_url": llm_cfg.get("base_url") or "",
                "api_key_env": api_key_env,
                "api_key_set": bool(self._session_api_key or os.environ.get(api_key_env)),
                "configured": provider_name in ("mock", "")
                or bool(self._session_api_key or os.environ.get(api_key_env)),
                "mock": provider_name in ("mock", ""),
                "routing": self.config.get("router") or {},
                "usage": getattr(router, "usage", lambda: {})()
                if callable(getattr(router, "usage", None))
                else {},
            }
        )
        return status

    def set_llm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """热更新模型配置（Web「模型设置」页），无需重启服务。

        API Key 只进入 ``self._session_api_key``，不写回配置文件；
        用户关闭服务或刷新页面后需要重新输入。
        """
        payload = dict(payload or {})
        api_key = str(payload.pop("api_key", "") or "").strip()
        if api_key:
            self._session_api_key = api_key
        llm_cfg = dict(self.config.get("llm") or {})
        for key in ("provider", "base_url", "model", "timeout_s"):
            if key in payload and payload[key] not in (None, ""):
                llm_cfg[key] = payload[key]
        llm_cfg.pop("api_key", None)
        self.config["llm"] = llm_cfg
        effective = dict(llm_cfg)
        effective["api_key"] = self._session_api_key
        reconfigure = getattr(self.llm, "configure", None)
        if callable(reconfigure):
            try:
                reconfigure(effective)
            except Exception as exc:  # pragma: no cover - 热更新失败则重建
                log.warning("模型配置热更新失败，重建提供方：%s", exc)
                self.llm = self._build_llm()
        else:
            self.llm = self._build_llm()
        self._persist_config()
        status = self.llm_status()
        if payload.get("test"):
            tester = getattr(self.llm, "ping", None) or getattr(getattr(self.llm, "provider", None), "ping", None)
            if callable(tester):
                try:
                    status["test"] = tester()
                except Exception as exc:  # pragma: no cover
                    status["test"] = {"ok": False, "error": str(exc)}
        return status

    def _persist_config(self) -> None:
        """把运行时修改写回 ``config/platform.json``（演示环境友好）。"""
        path = self.root / "config" / "platform.json"
        serializable = json.loads(json.dumps(self.config, ensure_ascii=False))
        llm_section = serializable.get("llm")
        if isinstance(llm_section, dict):
            llm_section.pop("api_key", None)
        try:
            path.write_text(
                json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            log.warning("保存平台配置失败：%s", exc)

    # ------------------------------------------------------------------
    # 演示辅助
    # ------------------------------------------------------------------
    @property
    def loop_running(self) -> bool:
        return self.started

    def metrics_snapshot(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        runs = self.list_runs(200)
        tokens = 0
        by_status: dict[str, int] = {}
        for item in runs:
            status = str(item.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
            try:
                tokens += int(metrics.get("tokens_total") or 0)
            except (TypeError, ValueError):
                continue
        snapshot.update(
            {"runs_known": len(runs), "runs_by_status": by_status, "tokens_total": tokens}
        )
        return snapshot

    # ------------------------------------------------------------------
    # 场景目录（面向最终用户的分析工作台）
    # ------------------------------------------------------------------
    def _scenario_rows(self, dataset: str) -> int:
        path = Path(str(dataset or ""))
        if not path.is_absolute():
            path = self.root / path
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                return max(0, sum(1 for _ in fh) - 1)
        except OSError:
            return 0

    def list_scenarios(self) -> list[dict[str, Any]]:
        """列出可运行的场景（内置演示 + scenarios/*/scenario.json）。"""
        items: list[dict[str, Any]] = []
        default_wf = self.workflows.get("wf-ai4s-air-quality")
        if default_wf is not None:
            dataset = "examples/datasets/air_quality.csv"
            items.append(
                {
                    "id": "ai4s-air-quality",
                    "name": "空气质量与气象因子归因",
                    "title": "AI4S 空气质量归因分析",
                    "description": "分析 PM2.5 与气温、湿度、风速、SO₂ 的关系，"
                    "输出系数、拟合优度与验证证据，演示「首轮失败→自愈→通过」闭环。",
                    "kind": "single",
                    "domain": "科研 / 环境",
                    "workflow_id": "wf-ai4s-air-quality",
                    "workflow_name": default_wf.name,
                    "dataset": dataset,
                    "rows": self._scenario_rows(dataset),
                    "targets": ["pm25"],
                    "target_count": 1,
                    "nodes": len(default_wf.nodes),
                    "demo": True,
                }
            )
        scenarios_dir = self.root / "scenarios"
        if scenarios_dir.is_dir():
            for meta_path in sorted(scenarios_dir.glob("*/scenario.json")):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(meta, dict):
                    continue
                workflow_id = str(meta.get("workflow_id") or "")
                wf = self.workflows.get(workflow_id)
                targets = meta.get("targets")
                if not isinstance(targets, list) or not targets:
                    targets = [meta["target"]] if meta.get("target") else []
                targets = [str(item) for item in targets]
                dataset = str(meta.get("dataset") or "")
                items.append(
                    {
                        "id": str(meta.get("id") or meta_path.parent.name),
                        "name": str(meta.get("name") or meta.get("title") or meta_path.parent.name),
                        "title": str(meta.get("title") or meta.get("name") or ""),
                        "description": str(meta.get("description") or meta.get("task") or ""),
                        "kind": "multi" if len(targets) > 1 else "single",
                        "domain": str(meta.get("domain") or ""),
                        "workflow_id": workflow_id or f"wf-{meta_path.parent.name}",
                        "workflow_name": getattr(wf, "name", "") or workflow_id,
                        "dataset": dataset,
                        "rows": self._scenario_rows(dataset),
                        "targets": targets,
                        "target_count": len(targets),
                        "nodes": len(getattr(wf, "nodes", []) or []),
                        "demo": False,
                    }
                )
        return items

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "started": self.started,
            "uptime_s": round(time.time() - self.started_at, 1),
            "runs": len(self.store.list(1)),
            "workflows": list(self.workflows),
            "agents": len(self._agent_dicts()),
            "tools": len(self.list_tools().get("tools") or []),
            "events": self.bus.stats(),
            "llm": self.llm_status().get("provider"),
            "scheduler": self.scheduler_status().get("policy"),
        }

    def close(self) -> None:
        self.shutdown()


# ---------------------------------------------------------------------------
# 降级实现（子系统缺失时保证平台仍可启动、前端仍有数据）
# ---------------------------------------------------------------------------
class _SimpleAgentRegistry:
    """``agentweave.agents`` 尚未就位时的最小可用 Agent 注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_file(cls, path: Path) -> "_SimpleAgentRegistry":
        registry = cls()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            items = data.get("agents") if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                try:
                    spec = AgentSpec.from_dict(item)
                except Exception:
                    continue
                registry._specs[spec.id] = spec
        return registry

    def get(self, agent_id: str) -> AgentSpec | None:
        return self._specs.get(agent_id)

    def list(self) -> list[AgentSpec]:
        return list(self._specs.values())

    def register(self, spec: AgentSpec) -> AgentSpec:
        with self._lock:
            self._specs[spec.id] = spec
        return spec

    def load(self) -> dict[str, Any]:  # pragma: no cover - 仅用于负载视图
        return {agent_id: {"active": 0, "max": spec.max_iterations} for agent_id, spec in self._specs.items()}


class _NullProvider:
    """没有可用 LLM 提供方时的兜底：返回结构化错误而不是崩溃。"""

    name = "unavailable"
    model = ""

    async def complete(self, messages: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise RuntimeError("没有可用的模型提供方，请在「模型设置」页配置 API Key")


class _NullVerifier:
    """验证流水线缺失时的兜底：单检查项通过，避免引擎中断。"""

    def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        from ..core.types import VerifyCheckResult, VerifyResult

        return VerifyResult(
            passed=True,
            confidence=0.5,
            checks=[VerifyCheckResult(name="unavailable", passed=True, detail="验证模块未就位")],
            feedback="",
        )

    def available(self) -> list[str]:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def create_platform(config: dict[str, Any] | None = None, root: str | Path | None = None) -> Platform:
    """工厂函数（``server.create_platform`` 优先调用它）。"""
    payload = dict(config or {})
    if isinstance(payload.get("platform_config"), dict):
        payload = payload["platform_config"]
    return Platform(config=payload, root=root)


build_platform = create_platform


__all__ = [
    "Platform",
    "create_platform",
    "build_platform",
    "load_platform_config",
    "load_runtime_env",
    "apply_llm_env_overrides",
    "ENV_FILE_PATH",
    "DEFAULT_PLATFORM_CONFIG",
]
