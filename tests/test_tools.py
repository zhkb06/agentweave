"""工具层测试：内置工具、路径安全、python_exec 沙箱、MCP stdio 协议。

覆盖点
------
1. ``build_tool_registry`` 注册 9 个内置工具，``ToolRegistry.call`` 的参数校验、
   未知工具、超时与异常兜底、高风险审批。
2. ``dataset_stats`` / ``file_read`` / ``file_write`` / ``python_exec`` /
   ``calculator`` / ``json_query`` / ``chart_svg`` / ``report_render`` 的行为；
   路径穿越拦截、代码导入白名单、子进程超时与输出截断。
3. ``MCPClient`` 的 JSON-RPC 2.0 stdio 往返与“未配置时优雅未连接”。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentweave.core.types import ToolSpec
from agentweave.tools.builtin import (
    ALLOWED_IMPORTS,
    MAX_OUTPUT_BYTES,
    build_tool_registry,
)
from agentweave.tools.mcp_client import (
    MCPClient,
    MCPError,
    load_manager_from_config,
)
from agentweave.tools.registry import ToolRegistry

BUILTIN_TOOL_NAMES = {
    "python_exec",
    "calculator",
    "dataset_stats",
    "chart_svg",
    "file_read",
    "file_write",
    "http_get",
    "json_query",
    "report_render",
}


class ToolRegistryTest(unittest.IsolatedAsyncioTestCase):
    """工具注册表通用行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.registry = build_tool_registry(str(self.workspace))

    async def test_builtin_tools_registered(self) -> None:
        """9 个内置工具全部注册，specs/openai_tools 可导出。"""
        names = set(self.registry.names)
        self.assertEqual(names, BUILTIN_TOOL_NAMES)
        self.assertEqual(len(self.registry.specs()), 9)
        schemas = self.registry.openai_tools(["calculator", "python_exec"])
        self.assertEqual(len(schemas), 2)
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertIn(schema["function"]["name"], {"calculator", "python_exec"})
            self.assertIn("parameters", schema["function"])

    async def test_unknown_tool(self) -> None:
        """未知工具返回失败而非抛异常。"""
        result = await self.registry.call("not_exists", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)

    async def test_missing_required_argument(self) -> None:
        """缺少必填参数时返回校验失败。"""
        result = await self.registry.call("calculator", {})
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.error)

    async def test_high_risk_requires_approval(self) -> None:
        """require_approval=True 时高风险工具被拒绝。"""
        registry = ToolRegistry(workspace_dir=self.tmp.name, require_approval=True)
        registry.register(
            ToolSpec(
                name="danger",
                description="高风险工具",
                parameters={"type": "object", "properties": {}},
                risk_level="high",
            ),
            lambda: "boom",
        )
        result = await registry.call("danger", {})
        self.assertFalse(result.ok)
        self.assertIn("人工审批", result.error)

    async def test_tool_exception_captured(self) -> None:
        """工具内部异常被捕获为 ToolResult.error。"""

        def _explode() -> None:
            raise RuntimeError("内部错误")

        registry = ToolRegistry(workspace_dir=self.tmp.name)
        registry.register(
            ToolSpec(name="boom", description="抛错工具", parameters={"type": "object", "properties": {}}),
            _explode,
        )
        result = await registry.call("boom", {})
        self.assertFalse(result.ok)
        self.assertIn("内部错误", result.error)
        self.assertTrue(result.duration_s >= 0.0)


class BuiltinToolsTest(unittest.IsolatedAsyncioTestCase):
    """各内置工具的具体行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.registry = build_tool_registry(str(self.workspace))

    async def _call(self, name: str, args: dict[str, Any]):
        return await self.registry.call(name, args)

    async def test_calculator(self) -> None:
        """四则运算与数学函数。"""
        result = await self._call("calculator", {"expression": "1 + 2 * 3"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["result"], 7)
        result = await self._call("calculator", {"expression": "sqrt(16) + max(1, 2)"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["result"], 6)

    async def test_calculator_rejects_code(self) -> None:
        """计算器拒绝导入/调用等越权表达式。"""
        result = await self._call("calculator", {"expression": "__import__('os')"})
        self.assertFalse(result.ok)

    async def test_file_write_read_roundtrip(self) -> None:
        """写入后读回同一内容（含中文）。"""
        written = await self._call("file_write", {"path": "notes/报告.txt", "content": "空气质量分析：OK"})
        self.assertTrue(written.ok, written.error)
        self.assertGreater(written.value["bytes"], 0)
        read = await self._call("file_read", {"path": "notes/报告.txt"})
        self.assertTrue(read.ok, read.error)
        self.assertEqual(read.value["content"], "空气质量分析：OK")
        self.assertFalse(read.value["truncated"])

    async def test_file_read_missing(self) -> None:
        """读取不存在的文件返回失败。"""
        result = await self._call("file_read", {"path": "nope.txt"})
        self.assertFalse(result.ok)

    async def test_path_traversal_blocked(self) -> None:
        """路径穿越（../）被拦截。"""
        result = await self._call("file_write", {"path": "../escape.txt", "content": "x"})
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertFalse((Path(self.tmp.name).parent / "escape.txt").exists())
        result = await self._call("file_read", {"path": "../../etc/passwd"})
        self.assertFalse(result.ok)

    async def test_dataset_stats(self) -> None:
        """列统计：行数、列名、数值均值与缺失值。"""
        csv_text = "city,pm25,temp\nA,80,20\nB,90,22\nC,,24\n"
        written = await self._call("file_write", {"path": "data/air.csv", "content": csv_text})
        self.assertTrue(written.ok, written.error)
        result = await self._call("dataset_stats", {"path": "data/air.csv"})
        self.assertTrue(result.ok, result.error)
        value = result.value
        self.assertEqual(value["rows"], 3)
        self.assertEqual(set(value["numeric_columns"]), {"pm25", "temp"})
        self.assertEqual(value["missing_total"], 1)
        columns = {column["name"]: column for column in value["columns"]}
        self.assertAlmostEqual(columns["temp"]["mean"], 22.0, places=6)

    async def test_python_exec_prints_json(self) -> None:
        """python_exec 捕获 stdout 并解析最后一行 JSON。"""
        code = 'import json\nprint(json.dumps({"x": 42, "ok": True}))\n'
        result = await self._call("python_exec", {"code": code})
        self.assertTrue(result.ok, result.error)
        value = result.value
        self.assertEqual(value["exit_code"], 0)
        self.assertEqual(value["value_json"], {"x": 42, "ok": True})
        self.assertIn("42", value["stdout"])

    async def test_python_exec_timeout(self) -> None:
        """python_exec 超时返回失败（含“超时”说明）。"""
        code = "import time\ntime.sleep(3)\nprint('done')\n"
        result = await self._call("python_exec", {"code": code, "timeout_s": 1})
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)

    async def test_python_exec_blocks_network_import(self) -> None:
        """python_exec 拒绝网络/子进程等危险导入与调用。"""
        result = await self._call("python_exec", {"code": "import socket\nprint('x')\n"})
        self.assertFalse(result.ok)
        self.assertIn("禁止", result.error)
        result = await self._call("python_exec", {"code": "import os\nos.system('echo hi')\n"})
        self.assertFalse(result.ok)
        self.assertIn("禁止", result.error)
        result = await self._call("python_exec", {"code": "eval('1+1')\n"})
        self.assertFalse(result.ok)

    async def test_python_exec_output_truncated(self) -> None:
        """超长输出按 8KB 截断。"""
        code = "print('x' * 20000)\n"
        result = await self._call("python_exec", {"code": code})
        self.assertTrue(result.ok, result.error)
        stdout = result.value["stdout"]
        self.assertLessEqual(len(stdout.encode("utf-8")), MAX_OUTPUT_BYTES + 200)
        self.assertTrue(result.value["truncated"])

    async def test_python_exec_sandbox_lists(self) -> None:
        """白名单模块可导入，黑名单模块不在白名单中。"""
        self.assertIn("math", ALLOWED_IMPORTS)
        self.assertIn("statistics", ALLOWED_IMPORTS)
        self.assertNotIn("socket", ALLOWED_IMPORTS)
        self.assertNotIn("subprocess", ALLOWED_IMPORTS)

    async def test_json_query(self) -> None:
        """按点号/下标路径取值。"""
        data = {"a": {"b": [10, 20]}, "name": "空气质量"}
        result = await self._call("json_query", {"data": data, "path": "a.b[1]"})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.value["found"])
        self.assertEqual(result.value["value"], 20)
        missing = await self._call("json_query", {"data": data, "path": "a.b[9]"})
        self.assertFalse(missing.value["found"])

    async def test_chart_svg(self) -> None:
        """生成 SVG 柱状图并可写入文件。"""
        result = await self._call(
            "chart_svg",
            {"title": "系数对比", "data": {"temp": -0.84, "wind": -1.67}, "path": "charts/coef.svg"},
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("<svg", result.value["svg"])
        self.assertTrue((self.workspace / "charts" / "coef.svg").exists())
        empty = await self._call("chart_svg", {"data": []})
        self.assertFalse(empty.ok)

    async def test_report_render(self) -> None:
        """结构化结果渲染为 Markdown 并写入文件。"""
        result = await self._call(
            "report_render",
            {
                "title": "空气质量归因报告",
                "summary": "wind 是主导因子",
                "metrics": {"R²": 0.72, "n": 2000},
                "conclusion": "结论：wind 系数为 -1.67。",
                "filename": "reports/report.md",
            },
        )
        self.assertTrue(result.ok, result.error)
        markdown = result.value["markdown"]
        self.assertIn("# 空气质量归因报告", markdown)
        self.assertIn("## 关键指标", markdown)
        self.assertIn("## 结论", markdown)
        self.assertTrue((self.workspace / "reports" / "report.md").exists())
        self.assertEqual(result.value["chars"], len(markdown))


class MCPClientTest(unittest.IsolatedAsyncioTestCase):
    """MCP stdio 客户端。"""

    async def test_unconfigured_is_graceful(self) -> None:
        """未配置服务器时不抛异常，返回未连接状态。"""
        client = MCPClient(name="empty")
        self.assertFalse(client.configured)
        self.assertFalse(client.connect())
        self.assertEqual(client.list_tools(), [])
        with self.assertRaises(MCPError):
            client.call_tool("echo", {})
        status = client.status()
        self.assertFalse(status["connected"])
        client.close()

    async def test_stdio_roundtrip(self) -> None:
        """与假 MCP 服务器完成 initialize / tools/list / tools/call 往返。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server_script = Path(tmp.name) / "fake_mcp_server.py"
        server_script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")

        client = MCPClient(
            name="fake",
            command=sys.executable,
            args=[str(server_script)],
            timeout_s=10,
        )
        self.addCleanup(client.close)
        self.assertTrue(client.connect(), client.last_error)
        tools = client.list_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "echo")
        result = client.call_tool("echo", {"text": "hi"})
        self.assertEqual(result["content"][0]["text"], "echo:hi")
        self.assertTrue(client.status()["connected"])

    async def test_manager_without_servers(self) -> None:
        """空 mcp_servers 配置得到空管理器，connect_all 返回空映射。"""
        manager = load_manager_from_config({"mcp_servers": []})
        self.assertEqual(len(manager), 0)
        self.assertEqual(manager.connect_all(), {})
        self.assertEqual(manager.list_tools(), [])
        manager.close_all()


_FAKE_MCP_SERVER = '''"""假 MCP 服务器：最小 JSON-RPC 2.0 stdio 实现，用于单元测试。"""
import json
import sys


def reply(msg_id, result):
    """写回一条 JSON-RPC 响应（单行 JSON）。"""
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:  # 通知，无需响应
        continue
    if method == "initialize":
        reply(msg_id, {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1.0"}})
    elif method == "tools/list":
        reply(
            msg_id,
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "回声工具",
                        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    }
                ]
            },
        )
    elif method == "tools/call":
        arguments = (message.get("params") or {}).get("arguments") or {}
        reply(msg_id, {"content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}]})
    else:
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown method"}}) + "\\n"
        )
        sys.stdout.flush()
'''


if __name__ == "__main__":
    unittest.main()
