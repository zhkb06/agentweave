"""AgentWeave Web 服务启动入口。

用法::

    py -3.14 -m agentweave.api.server
    py -3.14 -m agentweave.api.server --port 8766 --no-browser
    py -3.14 -m agentweave.api.server --host 0.0.0.0 --web-dir web

启动流程：读取 ``config/platform.json`` → 构造平台门面 → ``create_app`` 组装
HTTP 服务 → 打印访问地址 → ``serve_forever``。Ctrl+C 优雅退出。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Sequence

from .app import DEFAULT_CONFIG, REPO_ROOT, WebApp, create_app

log = logging.getLogger("agentweave.server")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_platform_config(path: Path | str | None = None) -> dict[str, Any]:
    """读取平台配置并展开为 Web 服务配置（保留原始 llm/scheduler 等段落）。

    读取前会加载仓库根目录 ``.env``（若存在），随后 ``AGENTWEAVE_PROVIDER`` /
    ``AGENTWEAVE_BASE_URL`` / ``AGENTWEAVE_MODEL`` 覆盖 ``llm`` 段；API Key 只经
    环境变量（默认 ``AGENTWEAVE_API_KEY``）读取，绝不写入配置文件。
    """
    from ..runtime.platform import apply_llm_env_overrides, load_runtime_env

    load_runtime_env()
    config_path = Path(path) if path else REPO_ROOT / "config" / "platform.json"
    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError) as exc:
            log.warning("读取配置 %s 失败：%s", config_path, exc)
    applied = apply_llm_env_overrides(raw)
    if applied:
        log.info("已按环境变量覆盖 LLM 配置：%s", ", ".join(sorted(applied)))
    server_cfg = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    runtime_cfg = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    scheduler_cfg = raw.get("scheduler") if isinstance(raw.get("scheduler"), dict) else {}
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    cfg.update(
        {
            "host": str(server_cfg.get("host") or "127.0.0.1"),
            "port": int(server_cfg.get("port") or 8765),
            "auto_open_browser": bool(server_cfg.get("auto_open_browser", False)),
            "config_path": str(config_path),
            "platform_config": raw,
            "agent_max_concurrency": int(scheduler_cfg.get("agent_max_concurrency") or 2),
            "auto_approve": bool(runtime_cfg.get("auto_approve", True)),
            "auth_token": str(server_cfg.get("auth_token") or ""),
            "limits": raw.get("limits") if isinstance(raw.get("limits"), dict) else {},
            "logging": raw.get("logging") if isinstance(raw.get("logging"), dict) else {},
        }
    )
    return cfg


# ---------------------------------------------------------------------------
# 平台门面
# ---------------------------------------------------------------------------
def create_platform(config: dict[str, Any]) -> Any:
    """实例化 ``agentweave.runtime.platform.Platform``（门面由 Lead 实现）。"""
    try:
        from ..runtime import platform as platform_module
    except ImportError as exc:  # pragma: no cover - 依赖装配阶段
        raise RuntimeError(
            "未找到平台门面模块 agentweave.runtime.platform，请确认平台实现已就位"
        ) from exc

    factory = getattr(platform_module, "create_platform", None)
    if callable(factory):
        return factory(config)
    factory = getattr(platform_module, "build_platform", None)
    if callable(factory):
        return factory(config)

    cls = getattr(platform_module, "Platform", None)
    if not isinstance(cls, type):
        raise RuntimeError("agentweave.runtime.platform 未导出 Platform 类或 create_platform()")

    payload = config.get("platform_config") or config
    last_error: Exception | None = None
    for builder in (
        lambda: cls(payload),
        lambda: cls(config=payload),
        lambda: cls(),
    ):
        try:
            return builder()
        except TypeError as exc:  # 构造签名不匹配，尝试下一种
            last_error = exc
    raise RuntimeError(f"无法实例化平台门面 Platform：{last_error}")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _print_banner(host: str, port: int, web_dir: Path, platform: Any) -> None:
    url = f"http://{host}:{port}/"
    lines = [
        "",
        "  织流 AgentWeave · Web 控制台已启动",
        f"  访问地址   {url}",
        f"  静态目录   {web_dir}",
        f"  平台门面   {type(platform).__name__}",
        "  按 Ctrl+C 停止服务",
        "",
    ]
    for line in lines:
        print(line, flush=True)


def _open_browser_later(url: str, delay: float = 1.0) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - 环境无浏览器
            log.debug("自动打开浏览器失败", exc_info=True)

    threading.Thread(target=_open, name="agentweave-open-browser", daemon=True).start()


def _configure_logging(config: dict[str, Any]) -> None:
    # 控制台 + 可选轮转文件日志；级别优先级：环境变量 > 配置 > INFO
    level = (
        os.environ.get("AGENTWEAVE_LOG_LEVEL", "").upper()
        or str((config.get("logging") or {}).get("level") or "INFO").upper()
    )
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    logging_cfg = config.get("logging") if isinstance(config.get("logging"), dict) else {}
    file_name = str(logging_cfg.get("file") or "").strip()
    if not file_name:
        return
    path = Path(file_name)
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=int(logging_cfg.get("max_bytes") or 5_000_000),
            backupCount=int(logging_cfg.get("backup_count") or 3),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        log.info("日志文件：%s", path)
    except OSError as exc:  # pragma: no cover - 磁盘只读等
        log.warning("日志文件不可写 %s：%s", path, exc)


def _validate_config(config: dict[str, Any]) -> None:
    # 启动前做轻量配置校验：只告警不阻断，避免演示被环境差异卡住
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not (0 <= port <= 65535):
        log.warning("配置 port=%r 不在 0–65535 范围内，将回退默认端口", config.get("port"))
    web_dir = Path(str(config.get("web_dir") or DEFAULT_WEB_DIR))
    if not web_dir.is_dir():
        log.warning("web_dir 不存在：%s（静态页面将 404）", web_dir)
    token = str(config.get("auth_token") or "").strip()
    if token and len(token) < 16:
        log.warning("auth_token 长度 %s < 16，生产环境建议使用 32 位以上随机串", len(token))
    policy = str((config.get("scheduler") or {}).get("policy") or "") if isinstance(config.get("scheduler"), dict) else ""
    if policy:
        try:
            from ..scheduler.engine import normalize_policy

            normalize_policy(policy)
        except ValueError:
            log.warning("未知调度策略 %r，将使用调度器默认策略", policy)
        except ImportError:  # pragma: no cover - 调度器缺失时跳过校验
            pass


def serve(
    host: str | None = None,
    port: int | None = None,
    open_browser: bool | None = None,
    web_dir: str | os.PathLike[str] | None = None,
    config: dict[str, Any] | None = None,
    platform: Any = None,
) -> None:
    """启动 HTTP 服务（阻塞直到 Ctrl+C 或服务关闭）。

    :param platform: 传入已有平台实例（测试/嵌入场景）；省略则自动构造
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    cfg.update(load_platform_config(cfg.get("config_path")))
    if config:
        cfg.update(config)
    if host:
        cfg["host"] = host
    if port is not None:
        cfg["port"] = int(port)
    if open_browser is not None:
        cfg["auto_open_browser"] = bool(open_browser)
    if web_dir is not None:
        cfg["web_dir"] = str(Path(web_dir).resolve())

    _configure_logging(cfg)
    _validate_config(cfg)

    if platform is None:
        platform = create_platform(cfg)
    try:
        server = create_app(platform, cfg)
    except OSError as exc:
        raise SystemExit(
            f"无法绑定 {cfg.get('host')}:{cfg.get('port')}（{exc}）。"
            "端口可能已被占用，可用 --port 指定其它端口。"
        ) from exc

    app: WebApp = server.app  # type: ignore[attr-defined]
    bound_host, bound_port = server.server_address[0], int(server.server_address[1])
    _print_banner(str(bound_host), bound_port, app.web_dir, platform)
    if cfg.get("auto_open_browser"):
        _open_browser_later(f"http://{bound_host}:{bound_port}/")

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n  收到停止信号，正在关闭……", flush=True)
    finally:
        try:
            server.shutdown()
        except Exception:  # pragma: no cover - 关闭路径
            pass
        try:
            server.server_close()
        except Exception:  # pragma: no cover - 关闭路径
            pass
        app.shutdown()
        print("  AgentWeave Web 服务已停止。", flush=True)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentweave.api.server",
        description="织流 AgentWeave Web 控制台服务（纯标准库）",
    )
    parser.add_argument("--host", default=None, help="监听地址（默认取 config/platform.json）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    parser.add_argument("--web-dir", default=None, help="前端静态目录（默认仓库 web/）")
    parser.add_argument("--config", default=None, help="平台配置文件路径")
    parser.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器（默认不打开）"
    )
    parser.add_argument(
        "--open-browser", action="store_true", help="启动后自动打开浏览器"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    open_browser: bool | None = None
    if args.open_browser:
        open_browser = True
    if args.no_browser:
        open_browser = False
    serve(
        host=args.host,
        port=args.port,
        open_browser=open_browser,
        web_dir=args.web_dir,
        config={"config_path": args.config} if args.config else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
