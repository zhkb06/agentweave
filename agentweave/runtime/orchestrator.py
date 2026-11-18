"""运行编排器：负责一次运行的完整生命周期。

职责边界
--------
* **不含**工作流语义：节点怎么跑、什么时候验证、循环几轮，全部由
  ``agentweave.workflow.engine.WorkflowEngine`` 决定；
* 编排器只做「生命周期 + 上下文装配 + 结果归档」：
  1. 把运行标记为 RUNNING 并广播 ``run.started``；
  2. 装配 ``RuntimeContext``（事件总线、LLM、工具、Agent、验证流水线、调度器）；
  3. 调用执行引擎，捕获取消与异常，保证运行一定会进入终止态；
  4. 归档产物：工作流快照、分析脚本、验证报告、运行报告 ``report.md``；
  5. 广播 ``run.finished``（含指标），供 SSE 与前端运行监控页收尾。

为什么单独一层
--------------
执行引擎是可单测的纯逻辑；编排器承载「进程级副作用」（文件、事件、状态仓库）。
两者分离后，引擎测试不需要起 HTTP 服务，编排器测试也不需要真实的 LLM。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..core.types import (
    EventType,
    RunRequest,
    RunStatus,
    Workflow,
    new_id,
)
from .context import RuntimeContext
from .state import RunState

log = logging.getLogger("agentweave.orchestrator")


class Orchestrator:
    """一次运行的编排者；由 ``Platform`` 持有并复用。"""

    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.root: Path = platform.root

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def execute(self, run: RunState, wf: Workflow, request: RunRequest) -> dict[str, Any]:
        """执行一次运行；无论成功、失败、取消，都保证收尾。"""
        started = time.time()
        run.started_at = started
        run.status = RunStatus.RUNNING.value
        run.variables.setdefault("input", dict(request.inputs))
        for key, value in (request.inputs or {}).items():
            run.variables.setdefault(key, value)
        run.variables.setdefault("workflow_id", wf.id)
        run.variables.setdefault("run_id", run.run_id)
        # 数据集路径等场景变量：工作流自身 variables 优先，其次注入
        for key, value in (wf.variables or {}).items():
            run.variables.setdefault(key, value)

        ctx = self.build_context(run, wf, request)
        self.platform.bus.emit(
            EventType.RUN_STARTED,
            run_id=run.run_id,
            payload={
                "run_id": run.run_id,
                "workflow_id": wf.id,
                "workflow_name": wf.name,
                "policy": request.policy,
                "label": request.label,
                "inputs": _safe(request.inputs),
                "node_count": len(wf.nodes),
            },
        )
        ctx.log(run.run_id, f"运行开始：{wf.name}（{len(wf.nodes)} 个节点，策略 {request.policy}）")

        error = ""
        result: Any = None
        status = RunStatus.SUCCEEDED.value
        try:
            result = await self._invoke_engine(ctx, wf, run, request)
            status, error = self._interpret_result(result, run)
            if run.is_cancelled() and status not in (
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
            ):
                status = RunStatus.CANCELLED.value
                error = error or "运行已被用户取消"
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED.value
            error = "运行已取消"
            log.info("运行 %s 被取消", run.run_id)
        except Exception as exc:  # pragma: no cover - 兜底，异常不能吞
            status = RunStatus.FAILED.value
            error = f"{type(exc).__name__}: {exc}"
            log.exception("运行 %s 执行失败", run.run_id)
            try:
                ctx.emit(
                    EventType.RUN_ERROR,
                    node_id=None,
                    payload={"error": error, "phase": "engine"},
                    level="error",
                )
            except Exception:
                pass

        # 引擎可能提前把 run.status 置为终态；先退回 RUNNING，确保 wait_run / SSE
        # 客户端只有在 _finalize（产物、run.finished、store.finish）全部完成后
        # 才能观察到终态，避免“状态 succeeded 但 report.md 尚未落盘”的竞态。
        run.status = RunStatus.RUNNING.value
        try:
            self._finalize(ctx, run, wf, request, status, error, started)
        except Exception:  # pragma: no cover - 归档失败不应掩盖运行结果
            log.exception("运行 %s 归档失败", run.run_id)
            try:
                self.platform.store.finish(run, RunStatus.FAILED.value, "归档失败")
            except Exception:  # pragma: no cover
                log.exception("运行 %s 无法写入失败终态", run.run_id)
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error": run.error,
            "metrics": run.metrics(),
        }

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------
    def _run_scheduler(self, policy: str) -> Any:
        """按本次运行请求的策略派生调度器，让 per-run 策略真正生效。

        平台默认调度器被所有运行共享，直接改它的策略会让并发运行互相串策略；
        这里 ``fork`` 一个继承历史统计的副本，策略只影响本运行。
        策略非法时回退平台默认调度器（API 层已在入口拦截非法值）。
        """
        base = getattr(self.platform, "scheduler", None)
        name = str(policy or "").strip()
        fork = getattr(base, "fork", None)
        if base is not None and name and callable(fork):
            try:
                return fork(name)
            except ValueError:
                log.warning("运行策略 %s 非法，回退平台默认策略", name)
        return base

    def build_context(self, run: RunState, wf: Workflow, request: RunRequest) -> RuntimeContext:
        platform = self.platform
        ctx = RuntimeContext(
            bus=platform.bus,
            llm=platform.llm,
            tools=platform.tools,
            agents=platform.agents,
            verifier=None,
            scheduler=self._run_scheduler(request.policy),
            workflows=dict(platform.workflows),
            config=dict(platform.config),
            artifacts_dir=run.artifacts_dir,
            workspace_dir=str(platform.root),
            cancel_event=run.cancel_event,
            auto_approve=bool(request.auto_approve),
            run_id=run.run_id,
            platform=platform,
        )
        verifier = platform.make_verifier(ctx)
        bind = getattr(verifier, "set_context", None)
        if callable(bind):
            try:
                bind(ctx)
            except Exception:  # pragma: no cover - 上下文绑定失败不阻断
                log.debug("验证上下文绑定失败", exc_info=True)
        ctx.verifier = verifier
        # 工作流声明的变量作为上下文默认值（供 {{var}} 模板渲染）
        for key, value in (wf.variables or {}).items():
            ctx.config.setdefault(key, value)
        return ctx

    # ------------------------------------------------------------------
    # 引擎调用
    # ------------------------------------------------------------------
    async def _invoke_engine(
        self, ctx: RuntimeContext, wf: Workflow, run: RunState, request: RunRequest
    ) -> Any:
        engine_cls = _resolve_engine()
        engine = _instantiate_engine(engine_cls, ctx)
        execute = getattr(engine, "execute", None) or getattr(engine, "run", None)
        if not callable(execute):
            raise RuntimeError("WorkflowEngine 缺少 execute()/run() 方法")
        values: dict[str, Any] = {
            "ctx": ctx,
            "context": ctx,
            "workflow": wf,
            "wf": wf,
            "workflow_def": wf,
            "run": run,
            "run_state": run,
            "state": run,
            "request": request,
            "req": request,
            "inputs": dict(request.inputs or {}),
            "input": dict(request.inputs or {}),
            "variables": dict(request.inputs or {}),
        }
        result = _call_adaptively(execute, values)
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    def _interpret_result(result: Any, run: RunState) -> tuple[str, str]:
        """把引擎返回值归一为（状态, 错误）。"""
        status = ""
        error = ""
        if isinstance(result, dict):
            status = str(result.get("status") or "")
            error = str(result.get("error") or "")
        else:
            status = str(getattr(result, "status", "") or "")
            error = str(getattr(result, "error", "") or "")
        status = status.lower()
        if status in ("failed", "error", "rejected"):
            return RunStatus.FAILED.value, error or str(getattr(run, "error", "")) or "执行失败"
        if status in ("cancelled", "canceled"):
            return RunStatus.CANCELLED.value, error or "运行已取消"
        current = str(getattr(run, "status", "")).lower()
        if current in (RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value):
            return current, str(getattr(run, "error", "") or error)
        return RunStatus.SUCCEEDED.value, error

    # ------------------------------------------------------------------
    # 收尾归档
    # ------------------------------------------------------------------
    def _finalize(
        self,
        ctx: RuntimeContext,
        run: RunState,
        wf: Workflow,
        request: RunRequest,
        status: str,
        error: str,
        started: float,
    ) -> None:
        run.error = error or ""
        metrics = run.metrics()
        metrics["wall_s"] = round(time.time() - started, 3)
        run.finished_at = time.time()

        # 先落盘产物、再发结束事件、最后才把状态置为终态：
        # 外部（CLI/测试/SSE 客户端）一旦观察到终态，报告与产物必然已经就绪，
        # 不会出现“状态 succeeded 但 report.md 还没写出来”的竞态。
        report = self._build_report(run, wf, status, error, metrics)
        paths = self._save_artifacts(run, wf, report, metrics)

        self.platform.bus.emit(
            EventType.RUN_FINISHED,
            run_id=run.run_id,
            payload={
                "run_id": run.run_id,
                "status": status,
                "error": run.error,
                "duration_s": metrics.get("duration_s", 0),
                "wall_s": metrics["wall_s"],
                "success_rate": metrics.get("success_rate", 0),
                "tokens_total": metrics.get("tokens_total", 0),
                "self_loop_iterations": metrics.get("self_loop_iterations", 0),
                "avg_confidence": metrics.get("avg_confidence", 0),
                "verifications": len(run.verification),
                "report_path": paths.get("report", ""),
            },
            level="error" if status == RunStatus.FAILED.value else "info",
        )
        ctx.log(
            run.run_id,
            f"运行结束：{status}（耗时 {metrics['wall_s']}s，"
            f"Token {metrics.get('tokens_total', 0)}，自循环 {metrics.get('self_loop_iterations', 0)} 轮）",
            level="error" if status == RunStatus.FAILED.value else "info",
        )
        # 先落盘 summary.json，再写终态：观察者看到 succeeded 时，磁盘摘要已可用。
        try:
            summary = self.platform.store.to_summary(run)
            if hasattr(summary, "to_dict"):
                summary = summary.to_dict()
            if isinstance(summary, dict):
                summary["status"] = status
            directory = Path(run.artifacts_dir or (self.root / "data" / "runs" / run.run_id))
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:  # pragma: no cover - 持久化失败不影响运行结果
            log.warning("写入 summary.json 失败：%s", run.run_id, exc_info=True)
        # 终态最后写入：观察者看到 succeeded 时，产物与结束事件均已就绪。
        self._save_trace(run)
        self.platform.store.finish(run, status, run.error)

    def _save_artifacts(
        self, run: RunState, wf: Workflow, report: str, metrics: dict[str, Any]
    ) -> dict[str, str]:
        directory = Path(run.artifacts_dir or (self.root / "data" / "runs" / run.run_id))
        directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}

        def write(name: str, content: str, kind: str = "file") -> None:
            path = directory / name
            path.write_text(content, encoding="utf-8")
            paths[name] = str(path)
            run.add_artifact(name, str(path), kind)

        write("workflow.json", json.dumps(wf.to_dict(), ensure_ascii=False, indent=2), "workflow")
        write("metrics.json", json.dumps(metrics, ensure_ascii=False, indent=2, default=str), "metrics")
        write("report.md", report, "report")

        # 分析脚本：把 statistician 产出的代码留档，便于复现与审计
        for node_id, state in run.node_states.items():
            output = state.output
            code = _extract_code(output)
            if code:
                safe_node = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in node_id)
                write(f"analysis_{safe_node}.py", code, "code")
                break

        # 事件轨迹在 run.finished 事件之后再落盘（见 _save_trace），保证 events.jsonl 含结束事件
        return paths

    def _save_trace(self, run: RunState) -> None:
        """把本 run 的完整事件流写成 ``events.jsonl``（含 run.finished）。"""
        directory = Path(run.artifacts_dir or (self.root / "data" / "runs" / run.run_id))
        directory.mkdir(parents=True, exist_ok=True)
        trace = self.platform.get_events(run.run_id, 0, 5000)
        if not trace:
            return
        path = directory / "events.jsonl"
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in trace),
            encoding="utf-8",
        )
        run.add_artifact("events.jsonl", str(path), "trace")

    def _build_report(
        self, run: RunState, wf: Workflow, status: str, error: str, metrics: dict[str, Any]
    ) -> str:
        """生成 Markdown 运行报告；优先使用 writer Agent 的成稿。"""
        writer_markdown = ""
        for node_id, state in run.node_states.items():
            output = state.output
            text = _extract_markdown(output)
            if text:
                writer_markdown = text
                break

        lines = [
            f"# {wf.name} · 运行报告",
            "",
            f"- 运行 ID：`{run.run_id}`",
            f"- 工作流：`{wf.id}`（{len(wf.nodes)} 个节点）",
            f"- 调度策略：`{run.policy}`",
            f"- 状态：**{_status_label(status)}**",
            f"- 开始时间：{_fmt_time(run.started_at)}",
            f"- 结束时间：{_fmt_time(run.finished_at)}",
            f"- 总耗时：{metrics.get('duration_s', 0)} 秒",
            f"- Token 消耗：{metrics.get('tokens_total', 0)}（入 {metrics.get('tokens_in', 0)} / 出 {metrics.get('tokens_out', 0)}）",
            f"- 平均置信度：{metrics.get('avg_confidence', 0)}",
            f"- 自循环轮数：{metrics.get('self_loop_iterations', 0)}",
            f"- 验证次数：{metrics.get('verification_count', 0)}",
            "",
        ]
        if error:
            lines += ["## 失败原因", "", f"> {error}", ""]

        lines += ["## 节点执行轨迹", "", "| 节点 | Agent | 状态 | 尝试 | 置信度 | 耗时(s) |", "| --- | --- | --- | --- | --- | --- |"]
        for node_id, state in run.node_states.items():
            duration = 0.0
            if state.started_at:
                duration = max(0.0, (state.finished_at or state.started_at) - state.started_at)
            lines.append(
                f"| `{node_id}` | {state.agent_id or '-'} | {state.status} | {state.attempts} | "
                f"{state.confidence:.2f} | {duration:.2f} |"
            )
        lines.append("")

        if run.verification:
            lines += ["## 验证轨迹（五层验证）", ""]
            for index, record in enumerate(run.verification, 1):
                if not isinstance(record, dict):
                    continue
                passed = record.get("passed")
                mark = "✅" if passed else "❌"
                lines.append(
                    f"### {index}. {mark} 置信度 {record.get('confidence', '-')}"
                    f"（层：{record.get('layer', '-')}）"
                )
                for check in record.get("checks") or []:
                    if not isinstance(check, dict):
                        continue
                    flag = "✅" if check.get("passed") else "❌"
                    lines.append(
                        f"- {flag} **{check.get('name')}**：{check.get('detail', '')}"
                    )
                feedback = str(record.get("feedback") or "").strip()
                if feedback:
                    lines += ["", "> 反馈：" + feedback.replace("\n", "\n> "), ""]
                lines.append("")

        if run.self_loop:
            lines += ["## 自循环统计", "", "```json", json.dumps(run.self_loop, ensure_ascii=False, indent=2, default=str), "```", ""]

        if writer_markdown:
            lines += ["## 研究报告", "", writer_markdown, ""]

        lines += [
            "---",
            "",
            f"*本报告由织流 AgentWeave 自动生成（v{_version()}）。*",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _resolve_engine() -> Any:
    from .platform import _resolve  # 复用统一的候选导入逻辑，避免重复

    return _resolve(
        "WorkflowEngine", ("agentweave.workflow.engine", "agentweave.workflow")
    )


def _instantiate_engine(engine_cls: Any, ctx: RuntimeContext) -> Any:
    for attempt in ({"ctx": ctx}, {"context": ctx}, {"ctx": ctx, "max_steps": 500}):
        try:
            return engine_cls(**attempt)
        except TypeError:
            continue
    return engine_cls(ctx)  # 最后尝试位置参数


def _call_adaptively(fn: Any, values: dict[str, Any]) -> Any:
    """按参数名把值喂给 ``fn``；签名不可知时退化为常见位置参数组合。"""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover
        signature = None
    if signature is not None:
        kwargs: dict[str, Any] = {}
        ok = True
        for name, param in signature.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if name in values:
                kwargs[name] = values[name]
            elif param.default is param.empty:
                ok = False
                break
        if ok:
            try:
                return fn(**kwargs)
            except TypeError:
                pass
    attempts = [
        (),
        (values["workflow"], values["run"], values["inputs"]),
        (values["workflow"], values["run"]),
        (values["run"], values["workflow"]),
    ]
    last_error: Exception | None = None
    for args in attempts:
        try:
            return fn(*args)
        except TypeError as exc:
            last_error = exc
            continue
    raise last_error  # type: ignore[misc]


def _extract_markdown(output: Any) -> str:
    if isinstance(output, str) and len(output) > 120:
        return output
    if not isinstance(output, dict):
        return ""
    for key in ("markdown", "report", "content"):
        value = output.get(key)
        if isinstance(value, str) and len(value) > 120:
            return value
        if isinstance(value, dict):
            inner = value.get("markdown")
            if isinstance(inner, str) and len(inner) > 60:
                return inner
    nested = output.get("output")
    if isinstance(nested, dict):
        return _extract_markdown(nested)
    return ""


def _extract_code(output: Any) -> str:
    if isinstance(output, dict):
        code = output.get("code")
        if isinstance(code, str) and "\n" in code:
            return code
        content = output.get("content")
        if isinstance(content, dict) and isinstance(content.get("code"), str):
            return content["code"]
        nested = output.get("output")
        if isinstance(nested, dict):
            return _extract_code(nested)
    if isinstance(output, str) and ("def " in output or "import " in output):
        return output
    return ""


def _status_label(status: str) -> str:
    return {
        RunStatus.SUCCEEDED.value: "成功",
        RunStatus.FAILED.value: "失败",
        RunStatus.CANCELLED.value: "已取消",
        RunStatus.REJECTED.value: "已拒绝",
        RunStatus.RUNNING.value: "运行中",
    }.get(status, status or "未知")


def _fmt_time(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _safe(value: Any) -> Any:
    """把输入裁剪为可 JSON 序列化的安全结构（用于事件 payload）。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _version() -> str:
    from .. import __version__

    return __version__


__all__ = ["Orchestrator"]
