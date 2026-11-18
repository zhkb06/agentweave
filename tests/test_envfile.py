"""``.env`` 加载器与「环境变量覆盖 llm 配置」的回归测试。

覆盖：

1. 解析：注释 / 空行 / ``export`` 前缀 / 引号 / 行尾注释 / 空值 / 非法行 / BOM+CRLF；
2. 加载：写入给定 ``environ``、默认不覆盖已有变量、``override=True`` 可覆盖、
   文件缺失返回空列表；
3. 集成：``load_platform_config`` 在环境变量存在时覆盖 ``llm.provider/base_url/model``，
   且 API Key 不会被写进配置。

运行::

    PYTHONIOENCODING=utf-8 py -3.14 -m unittest tests.test_envfile -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentweave.core.envfile import load_env_file, parse_env_text  # noqa: E402
from agentweave.runtime.platform import (  # noqa: E402
    apply_llm_env_overrides,
    load_platform_config,
)


class ParseEnvTextTests(unittest.TestCase):
    def test_comments_blank_and_export(self) -> None:
        parsed = parse_env_text(
            "\n".join(
                [
                    "# 注释行",
                    "",
                    "AGENTWEAVE_MODEL=deepseek-chat",
                    "export AGENTWEAVE_BASE_URL=https://api.deepseek.com/v1",
                    "   ",
                    "NOT_A_PAIR",
                ]
            )
        )
        self.assertEqual(
            parsed,
            {
                "AGENTWEAVE_MODEL": "deepseek-chat",
                "AGENTWEAVE_BASE_URL": "https://api.deepseek.com/v1",
            },
        )

    def test_quotes_and_inline_comment(self) -> None:
        parsed = parse_env_text(
            "\n".join(
                [
                    'QUOTED_DOUBLE="sk-abc#123"',
                    "QUOTED_SINGLE='a b c'",
                    "INLINE=value # 行尾注释",
                    "HASH_NO_SPACE=sk-ab#cd",
                ]
            )
        )
        self.assertEqual(parsed["QUOTED_DOUBLE"], "sk-abc#123")
        self.assertEqual(parsed["QUOTED_SINGLE"], "a b c")
        self.assertEqual(parsed["INLINE"], "value")
        self.assertEqual(parsed["HASH_NO_SPACE"], "sk-ab#cd")

    def test_empty_values_and_invalid_lines_skipped(self) -> None:
        parsed = parse_env_text(
            "\n".join(
                [
                    "EMPTY=",
                    "=NOKEY",
                    "BAD KEY=v",
                    "  =  ",
                    "OK=1",
                ]
            )
        )
        self.assertEqual(parsed, {"OK": "1"})

    def test_bom_and_crlf(self) -> None:
        parsed = parse_env_text("\ufeffA=1\r\nB=2\r\n")
        self.assertEqual(parsed, {"A": "1", "B": "2"})


class LoadEnvFileTests(unittest.TestCase):
    def test_writes_missing_and_reports_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=1\nB=2\n", encoding="utf-8")
            environ: dict[str, str] = {"A": "real-env"}
            applied = load_env_file(path, environ=environ)
            self.assertEqual(environ["A"], "real-env")  # 真实环境变量优先
            self.assertEqual(environ["B"], "2")
            self.assertEqual(applied, ["B"])

    def test_override_true_replaces_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=from-file\n", encoding="utf-8")
            environ: dict[str, str] = {"A": "real-env"}
            applied = load_env_file(path, environ=environ, override=True)
            self.assertEqual(environ["A"], "from-file")
            self.assertEqual(applied, ["A"])

    def test_missing_file_is_noop(self) -> None:
        environ: dict[str, str] = {}
        self.assertEqual(load_env_file(Path("/definitely/not/exists/.env"), environ=environ), [])
        self.assertEqual(load_env_file(None, environ=environ), [])
        self.assertEqual(environ, {})


class ConfigEnvOverrideTests(unittest.TestCase):
    def _write_config(self, tmp: str) -> Path:
        path = Path(tmp) / "platform.json"
        path.write_text(
            json.dumps(
                {
                    "llm": {
                        "provider": "mock",
                        "base_url": "https://config.example/v1",
                        "model": "config-model",
                        "api_key_env": "AGENTWEAVE_API_KEY",
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_env_overrides_llm_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(tmp)
            env = {
                "AGENTWEAVE_PROVIDER": "openai_compat",
                "AGENTWEAVE_BASE_URL": "https://api.deepseek.com/v1",
                "AGENTWEAVE_MODEL": "deepseek-chat",
            }
            with mock.patch.dict(os.environ, env):
                cfg = load_platform_config(config_path)
            llm = cfg["llm"]
            self.assertEqual(llm["provider"], "openai_compat")
            self.assertEqual(llm["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(llm["model"], "deepseek-chat")
            self.assertNotIn("api_key", llm)

    def test_apply_overrides_ignores_empty_values(self) -> None:
        raw: dict[str, object] = {"llm": {"provider": "mock", "model": "m1"}}
        with mock.patch.dict(os.environ, {"AGENTWEAVE_PROVIDER": "", "AGENTWEAVE_MODEL": "  "}):
            applied = apply_llm_env_overrides(raw)
        self.assertEqual(applied, {})
        self.assertEqual(raw["llm"], {"provider": "mock", "model": "m1"})

    def test_apply_overrides_creates_llm_section(self) -> None:
        raw: dict[str, object] = {}
        with mock.patch.dict(os.environ, {"AGENTWEAVE_MODEL": "glm-4-flash"}):
            applied = apply_llm_env_overrides(raw)
        self.assertEqual(applied, {"AGENTWEAVE_MODEL": "glm-4-flash"})
        self.assertEqual(raw["llm"], {"model": "glm-4-flash"})

    def test_repo_env_file_is_loaded_by_platform_config(self) -> None:
        """``load_platform_config`` 必须自己把 ``.env`` 加载进环境变量并覆盖 llm 段。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / ".env"
            env_path.write_text(
                "AGENTWEAVE_PROVIDER=openai_compat\nAGENTWEAVE_MODEL=from-env-file\n",
                encoding="utf-8",
            )
            config_path = self._write_config(str(tmp_path))
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTWEAVE_")}
            with mock.patch.dict(os.environ, clean_env, clear=True), mock.patch(
                "agentweave.runtime.platform.ENV_FILE_PATH", env_path
            ):
                cfg = load_platform_config(config_path)
                self.assertEqual(cfg["llm"]["provider"], "openai_compat")
                self.assertEqual(cfg["llm"]["model"], "from-env-file")
                self.assertEqual(os.environ.get("AGENTWEAVE_MODEL"), "from-env-file")


if __name__ == "__main__":
    unittest.main()
