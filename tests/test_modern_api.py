from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from enkiball import (
    BUILTIN_TOOL_NAMES,
    Enkiball,
    MCPClient,
    MCPServerConfig,
    SSEMCPServer,
    StdioMCPServer,
    StreamableHTTPMCPServer,
    ToolCallContext,
    ToolHook,
    ToolResultContext,
    generate_tool_schema,
)
from enkiball.config import save_config


class DummyMCPClient(MCPClient):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.running = False
        self.tools = [
            {
                "name": "dummy_tool",
                "description": "Dummy tool for test",
                "inputSchema": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                },
            }
        ]

    def _send_payload(self, payload: dict, cancel_event=None) -> None:
        pass

    def start(self) -> None:
        self.running = True

    def close(self) -> None:
        self.running = False

    def call_tool(self, tool_name: str, args: dict, cancel_event=None) -> str:
        return f"called {tool_name} with {args}"


class ModernAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "enki.json"
        save_config({}, self.config_path)

    def _create_agent(self, **kwargs) -> Enkiball:
        defaults = {
            "config_path": self.config_path,
            "state_path": self.root / "state.json",
            "session_dir": self.root / "sessions",
            "cwd": self.root,
            "persist": False,
            "skill_dirs": [],
        }
        defaults.update(kwargs)
        agent = Enkiball(**defaults)
        self.addCleanup(agent.shutdown)
        return agent

    # ------------------------------------------------------------------------
    # 1. MCP Programmatic API Tests
    # ------------------------------------------------------------------------

    def test_mcp_server_config_dataclasses(self) -> None:
        stdio = StdioMCPServer(
            command="node",
            args=["server.js"],
            environment={"FOO": "BAR"},
            transport="jsonl",
            enabled=True,
        )
        self.assertEqual(
            stdio.to_dict(),
            {
                "command": "node",
                "args": ["server.js"],
                "environment": {"FOO": "BAR"},
                "transport": "jsonl",
                "enabled": True,
            },
        )

        sse = SSEMCPServer(url="http://localhost:8000/sse", headers={"X-Key": "123"})
        self.assertEqual(
            sse.to_dict(),
            {
                "url": "http://localhost:8000/sse",
                "type": "sse",
                "headers": {"X-Key": "123"},
                "enabled": True,
            },
        )

        http = StreamableHTTPMCPServer(url="http://localhost:9000/mcp")
        self.assertEqual(
            http.to_dict(),
            {"url": "http://localhost:9000/mcp", "type": "streamable-http", "enabled": True},
        )

    def test_agent_add_and_remove_mcp_server(self) -> None:
        agent = self._create_agent()

        server_cfg = StdioMCPServer(command="echo", args=["test"])
        ok, msg = agent.add_mcp_server("test_echo", server_cfg)
        self.assertTrue(ok)
        self.assertIn("added", msg)
        self.assertIn("test_echo", agent.config["mcp"]["servers"])

        # Status check
        status = agent.mcp.status()
        self.assertEqual(status.get("test_echo"), "STOPPED")

        # Remove
        removed = agent.remove_mcp_server("test_echo")
        self.assertTrue(removed)
        self.assertNotIn("test_echo", agent.config.get("mcp", {}).get("servers", {}))

    def test_agent_register_mcp_client(self) -> None:
        agent = self._create_agent()
        dummy = DummyMCPClient("inproc_service")

        ok, msg = agent.register_mcp_client("inproc_service", dummy, start=True)
        self.assertTrue(ok)
        self.assertTrue(dummy.running)
        self.assertIn("1 tools", msg)

        # Check status and tools
        status = agent.mcp.status()
        self.assertEqual(status.get("inproc_service"), "RUNNING")

        tools = agent.mcp.get_tools()
        tool_names = [t["function"]["name"] for t in tools]
        self.assertIn("mcp__inproc_service__dummy_tool", tool_names)

        # Direct tool call via manager
        res = agent.mcp.call_tool("mcp__inproc_service__dummy_tool", {"arg": "hello"})
        self.assertEqual(res, "called dummy_tool with {'arg': 'hello'}")

    # ------------------------------------------------------------------------
    # 2. Modern Tools API Tests (Schema Derivation, Unpacking, Async, Builtin Tools)
    # ------------------------------------------------------------------------

    def test_generate_tool_schema_derivation(self) -> None:
        def my_function(
            city: str,
            count: int = 5,
            verbose: bool = False,
            ratio: float = 1.5,
            tags: list[str] = [],
            note: Optional[str] = None,
        ) -> str:
            """Query information for a given city.

            Args:
                city: Target city name.
                count: Number of records.
            """
            return city

        schema = generate_tool_schema(my_function)
        fn_schema = schema["function"]

        self.assertEqual(fn_schema["name"], "my_function")
        self.assertIn("Query information for a given city.", fn_schema["description"])

        params = fn_schema["parameters"]
        self.assertEqual(params["type"], "object")
        props = params["properties"]

        self.assertEqual(props["city"]["type"], "string")
        self.assertEqual(props["city"]["description"], "Target city name.")
        self.assertEqual(props["count"]["type"], "integer")
        self.assertEqual(props["count"]["default"], 5)
        self.assertEqual(props["verbose"]["type"], "boolean")
        self.assertEqual(props["ratio"]["type"], "number")
        self.assertEqual(props["tags"]["type"], "array")
        self.assertEqual(props["note"]["type"], "string")

        self.assertEqual(params["required"], ["city"])

    def test_agent_tool_decorator_and_kwargs_unpacking(self) -> None:
        agent = self._create_agent()

        @agent.tool
        def add_numbers(x: int, y: int = 10) -> int:
            """Add two numbers."""
            return x + y

        self.assertIn("add_numbers", agent.list_custom_tools())

        # Calling tool with kwargs unpacking and ignoring unexpected arguments
        result_str = agent._engine._dispatch_tool("add_numbers", {"x": 5, "y": 20, "extra": 999})
        self.assertIn('"ok": true', result_str)
        self.assertIn('"result": 25', result_str)

    def test_async_tool_support(self) -> None:
        agent = self._create_agent()

        @agent.tool
        async def async_math(val: int) -> dict[str, int]:
            """Compute double asynchronously."""
            await asyncio.sleep(0.01)
            return {"doubled": val * 2}

        # 1. Sync dispatch path runs coroutine smoothly
        res_sync = agent._engine._dispatch_tool("async_math", {"val": 21})
        self.assertIn('"doubled": 42', res_sync)

        # 2. Async dispatch path awaits natively
        turn = MagicMock()
        turn.turn_id = "test-turn"
        turn.cancel_event.is_set.return_value = False

        async def _test_async() -> str:
            return await agent._engine._adispatch_tool("async_math", {"val": 50}, turn)

        res_async = asyncio.run(_test_async())
        self.assertIn('"doubled": 100', res_async)

    def test_builtin_tools_configuration(self) -> None:
        # Default: all builtin tools present
        agent_default = self._create_agent()
        default_tool_names = {t["function"]["name"] for t in agent_default._engine._get_tools()}
        self.assertTrue(BUILTIN_TOOL_NAMES.issubset(default_tool_names))

        # builtin_tools = False: no builtin tools present
        agent_disabled = self._create_agent(builtin_tools=False)
        disabled_tool_names = {t["function"]["name"] for t in agent_disabled._engine._get_tools()}
        self.assertEqual(len(disabled_tool_names.intersection(BUILTIN_TOOL_NAMES)), 0)

        # Attempting to call disabled builtin tool returns error
        res = agent_disabled._engine._dispatch_tool("read_file", {"path": "dummy"})
        self.assertIn('"ok": false', res)
        self.assertIn("not enabled", res)

        # Selective builtin tools
        agent_selective = self._create_agent(builtin_tools=["read_file"])
        sel_names = {t["function"]["name"] for t in agent_selective._engine._get_tools()}
        self.assertIn("read_file", sel_names)
        self.assertNotIn("write_file", sel_names)
        self.assertNotIn("run_command", sel_names)

    # ------------------------------------------------------------------------
    # 3. Tool Lifecycle Hooks Tests
    # ------------------------------------------------------------------------

    def test_tool_lifecycle_hooks(self) -> None:
        before_events: list[ToolCallContext] = []
        after_events: list[ToolResultContext] = []

        agent = self._create_agent()

        agent.add_tool_hook(
            before=lambda ctx: before_events.append(ctx),
            after=lambda ctx: after_events.append(ctx),
        )

        @agent.tool
        def multiply(a: int, b: int) -> int:
            return a * b

        res = agent._engine._dispatch_tool("multiply", {"a": 6, "b": 7}, call_id="call-42")
        self.assertIn('"result": 42', res)

        # Verify before hook
        self.assertEqual(len(before_events), 1)
        self.assertEqual(before_events[0].name, "multiply")
        self.assertEqual(before_events[0].args, {"a": 6, "b": 7})
        self.assertEqual(before_events[0].call_id, "call-42")
        self.assertIs(before_events[0].agent, agent)

        # Verify after hook
        self.assertEqual(len(after_events), 1)
        self.assertEqual(after_events[0].name, "multiply")
        self.assertEqual(after_events[0].call_id, "call-42")
        self.assertGreater(after_events[0].duration_sec, 0.0)
        self.assertIsNone(after_events[0].error)
        self.assertIn('"result": 42', str(after_events[0].result))


if __name__ == "__main__":
    unittest.main()
