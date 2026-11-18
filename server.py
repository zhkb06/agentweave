#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""织流 AgentWeave · 一键启动入口。

用法::

    python server.py                  # 读取 config/platform.json，监听 127.0.0.1:8765
    python server.py --port 9000      # 覆盖端口
    python server.py --host 0.0.0.0   # 覆盖监听地址
    python server.py --no-browser     # 启动后不自动打开浏览器（默认不打开）

等价入口：``python -m agentweave.api.server``。
本文件只做参数解析、平台门面装配与 ``serve_forever``，业务逻辑全部在
``agentweave/api`` 包内，保证「python server.py」与模块方式行为一致。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

# 保证以脚本方式运行（python server.py）时也能导入仓库内包
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentweave.api.app import create_app  # noqa: E402
from agentweave.api.server import create_platform, load_platform_config  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="织流 AgentWeave Web 控制台（纯 Python 标准库，零第三方依赖）",
    )
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    parser.add_argument("--config", default=None, help="平台配置文件路径（默认 config/platform.json）")
    parser.add_argument("--web-dir", default=None, help="前端静态目录（默认仓库 web/）")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="启动后自动打开系统浏览器",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器（默认行为，保留该参数便于脚本调用）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="精简启动横幅（仅打印访问地址）",
    )
    return parser


def _banner(host: str, port: int, web_dir: Path, platform: object, quiet: bool) -> None:
    url = f"http://{host}:{port}/"
    if quiet:
        print(url, flush=True)
        return
    lines = [
        "",
        "  ┌──────────────────────────────────────────────┐",
        "  │   织流 AgentWeave · 多智能体工作流编排平台   │",
        "  └──────────────────────────────────────────────┘",
        f"  访问地址   {url}",
        f"  静态目录   {web_dir}",
        f"  平台门面   {type(platform).__name__}",
        "  提示       演示场景请点击「运行演示场景」，或访问 #/overview",
        "  停止服务   按 Ctrl+C",
        "",
    ]
    for line in lines:
        print(line, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认编码可能非 UTF-8，启动横幅含中文，容错处理
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):  # pragma: no cover - 非交互环境
        pass

    args = build_parser().parse_args(argv)

    config = load_platform_config(args.config)
    config["host"] = args.host or config.get("host") or DEFAULT_HOST
    config["port"] = int(args.port if args.port is not None else config.get("port") or DEFAULT_PORT)
    config["web_dir"] = str(Path(args.web_dir).resolve()) if args.web_dir else str(REPO_ROOT / "web")
    if args.open_browser:
        config["auto_open_browser"] = True
    elif args.no_browser:
        config["auto_open_browser"] = False

    platform = None
    server = None
    try:
        platform = create_platform(config)
        server = create_app(platform, config)
    except OSError as exc:
        print(
            f"  启动失败：无法绑定 {config['host']}:{config['port']}（{exc}）。\n"
            "  端口可能已被占用，可用 --port 指定其它端口，例如：python server.py --port 8766",
            file=sys.stderr,
            flush=True,
        )
        return 2
    except Exception as exc:  # pragma: no cover - 装配失败时给出可读提示
        print(f"  启动失败：{exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1

    bound_host, bound_port = server.server_address[0], int(server.server_address[1])
    app = server.app  # type: ignore[attr-defined]
    _banner(str(bound_host), bound_port, app.web_dir, platform, args.quiet)

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C，正在优雅停止……", flush=True)
    finally:
        for closer in (getattr(server, "shutdown", None), getattr(server, "server_close", None)):
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - 关闭路径尽力而为
                    pass
        try:
            app.shutdown()
        except Exception:  # pragma: no cover - 关闭路径尽力而为
            pass
        print("  AgentWeave Web 服务已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
