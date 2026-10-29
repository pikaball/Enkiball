from __future__ import annotations

import asyncio
import io
import json
import threading
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from enkiball.agent import Enkiball
from enkiball.cli import EnkiballCLI
from enkiball.commands import CommandContext, CommandHandler
from enkiball.llm import ChatEngine
from enkiball.mcp import MCPManager
from enkiball.session_store import SessionStore


class MessageStateTests(unittest.TestCase):
    def test_export_and_load_do_not_share_mutable_messages(self) -> None:
        engine = ChatEngine({})
        source = [{"role": "user", "content": {"nested": ["original"]}}]

        engine.load_messages(source)
        source[0]["content"]["nested"][0] = "caller mutation"
        exported = engine.export_messages()
        exported[-1]["content"]["nested"][0] = "export mutation"

        self.assertEqual(engine.messages[-1]["content"]["nested"], ["original"])

    def test_revert_recomputes_compact_index(self) -> None:
        engine = ChatEngine({})
        engine.load_messages(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
                {"role": "system", "content": "summary", "_compact": True},
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": "answer"},
            ]
        )

        engine.revert_user_turns(2)

        self.assertEqual(engine.compact_index, 0)
        self.assertEqual(engine.messages, [{"role": "system", "content": "system"}])

    def test_fork_rejects_invalid_turns(self) -> None:
        engine = ChatEngine({})
        engine.messages.append({"role": "user", "content": "only"})

        self.assertEqual(engine.fork_at_turn(0), [])
        self.assertEqual(engine.fork_at_turn(2), [])

    def test_auto_compact_uses_latest_context_usage(self) -> None:
        engine = ChatEngine({})
        engine.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "first",
                    "_usage": {"prompt_tokens": 100, "completion_tokens": 10},
                },
                {
                    "role": "assistant",
                    "content": "second",
                    "_usage": {"prompt_tokens": 130, "completion_tokens": 15},
                },
            ]
        )

        self.assertEqual(engine._api_visible_usage_total(), 145)

    def test_tool_arguments_must_decode_to_an_object(self) -> None:
        self.assertEqual(ChatEngine._parse_tool_args("[]"), {})
        self.assertEqual(ChatEngine._parse_tool_args("null"), {})
        self.assertEqual(ChatEngine._parse_tool_args('{"value": 1}'), {"value": 1})


class ToolRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ChatEngine({})
        self.schema = {
            "type": "function",
            "function": {
                "name": "custom",
                "description": "custom",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def test_registration_rejects_duplicates_and_reserved_names(self) -> None:
        self.engine.register_tool("custom", self.schema, lambda args: "ok")

        with self.assertRaisesRegex(ValueError, "already registered"):
            self.engine.register_tool("custom", self.schema, lambda args: "other")

        reserved = {
            **self.schema,
            "function": {**self.schema["function"], "name": "read_file"},
        }
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.engine.register_tool("read_file", reserved, lambda args: "bad")

class TurnOwnershipTests(unittest.TestCase):
    def test_concurrent_turn_is_rejected(self) -> None:
        engine = ChatEngine({})
        active = engine._begin_turn()
        self.addCleanup(engine._end_turn, active)

        with self.assertRaisesRegex(RuntimeError, "another turn"):
            engine._begin_turn()

    def test_interrupt_targets_active_turn_and_is_retained(self) -> None:
        engine = ChatEngine({})
        active = engine._begin_turn()

        engine.interrupt()
        engine._end_turn(active)

        self.assertTrue(engine.interrupted())

    def test_stream_reply_releases_turn_after_failure(self) -> None:
        engine = ChatEngine({})
        with patch.object(engine, "_client", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                engine.stream_reply("hello", None, lambda token: None)

        turn = engine._begin_turn()
        engine._end_turn(turn)

    def test_sync_stream_close_interrupts_and_joins_worker(self) -> None:
        from enkiball.agent import Enkiball

        agent = Enkiball.__new__(Enkiball)
        engine = ChatEngine({})
        agent._engine = engine
        agent._on_tool_event = None
        started = threading.Event()
        stopped = threading.Event()

        def fake_reply(*args, **kwargs) -> str:
            turn = engine._begin_turn()
            try:
                kwargs["on_token"]("first")
                started.set()
                while not kwargs["should_stop"]():
                    time.sleep(0.01)
                stopped.set()
                return "first"
            finally:
                engine._end_turn(turn)

        with patch.object(engine, "stream_reply", side_effect=fake_reply):
            stream = agent.stream("hello")
            self.assertEqual(next(stream), "first")
            self.assertTrue(started.is_set())
            stream.close()

        self.assertTrue(stopped.is_set())


class AgentCallbackTests(unittest.TestCase):
    def build_agent(self) -> Enkiball:
        agent = Enkiball.__new__(Enkiball)
        agent._engine = ChatEngine({})
        agent._on_tool_event = None
        return agent

    def test_chat_exposes_stream_callbacks_without_private_engine_access(self) -> None:
        agent = self.build_agent()
        tokens: list[str] = []
        tools: list[str] = []
        thinking: list[str] = []

        def fake_reply(**kwargs) -> str:
            kwargs["on_token"]("answer")
            kwargs["on_tool_event"]("tool({})")
            kwargs["on_thinking"]("reason")
            return "answer"

        with patch.object(agent._engine, "stream_reply", side_effect=fake_reply):
            result = agent.chat(
                "question",
                on_token=tokens.append,
                on_tool_event=tools.append,
                on_thinking=thinking.append,
            )

        self.assertEqual(result.content, "answer")
        self.assertEqual(tokens, ["answer"])
        self.assertEqual(tools, ["tool({})"])
        self.assertEqual(thinking, ["reason"])

    def test_astream_consumes_the_structured_event_stream(self) -> None:
        agent = self.build_agent()

        async def fake_reply(**kwargs) -> str:
            for callback_name, value in (
                ("on_token", "answer"),
                ("on_tool_event", "tool({})"),
                ("on_thinking", "reason"),
            ):
                callback_result = kwargs[callback_name](value)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            return "answer"

        async def collect() -> list[str]:
            return [token async for token in agent.astream("question")]

        with patch.object(agent._engine, "astream_reply", side_effect=fake_reply):
            tokens = asyncio.run(collect())

        self.assertEqual(tokens, ["answer"])

    def test_btw_restores_history_and_token_usage(self) -> None:
        agent = self.build_agent()
        agent._engine.load_messages(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "previous"},
            ]
        )
        original_messages = agent._engine.export_messages()
        agent._engine.token_usage.add(10, 2)
        original_usage = agent._engine.token_usage.to_dict()
        agent._engine._last_turn_interrupted = False

        def fake_reply(*args, **kwargs) -> str:
            agent._engine.messages.append({"role": "assistant", "content": "btw"})
            agent._engine.token_usage.add(100, 20)
            return "answer"

        with patch.object(agent._engine, "_stream_reply_impl", side_effect=fake_reply):
            result = agent.btw("question")

        self.assertEqual(result.content, "answer")
        self.assertEqual(agent._engine.export_messages(), original_messages)
        self.assertEqual(agent._engine.token_usage.to_dict(), original_usage)
        self.assertFalse(agent._engine.interrupted())


class ProviderAndMcpTests(unittest.TestCase):
    def test_provider_mapping_is_a_deep_copy_without_redaction(self) -> None:
        from enkiball.agent import Enkiball

        agent = Enkiball.__new__(Enkiball)
        agent._config = {
            "providers": {"one": {"api_key": "secret", "models": {"m": {}}}}
        }

        providers = agent.providers
        providers["one"]["models"]["m"]["temperature"] = 1

        self.assertEqual(providers["one"]["api_key"], "secret")
        self.assertEqual(agent._config["providers"]["one"]["models"]["m"], {})

    def test_mcp_enable_rolls_back_when_start_fails(self) -> None:
        from enkiball.agent import Enkiball

        agent = Enkiball.__new__(Enkiball)
        agent._config = {"mcp": {"servers": {"broken": {"enabled": False}}}}
        agent._mcp = MCPManager(agent._config)

        with patch.object(agent._mcp, "start", return_value=(False, "failed")):
            self.assertFalse(agent.mcp_enable("broken"))

        self.assertFalse(agent._config["mcp"]["servers"]["broken"]["enabled"])


class CommandExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SessionStore._from_paths(root / "sessions", root / "exports")
        self.agent = Enkiball.__new__(Enkiball)
        self.agent._engine = ChatEngine({}, state_path=root / "state.json")
        self.agent._config = {}
        self.agent._mcp = MCPManager({})
        self.agent._session_store = self.store
        self.agent._config_path = root / "config.json"
        self.agent._state_path = root / "state.json"
        self.agent.notice_queue = []
        self.handler = CommandHandler(
            Console(file=None, quiet=True),
            self.agent,
        )

    def test_context_handler_is_supported_and_legacy_handler_is_rejected(self) -> None:
        context_calls: list[tuple[list[str], CommandContext]] = []

        def context_handler(argv: list[str], context: CommandContext) -> bool:
            context_calls.append((argv, context))
            return True

        def legacy_handler(argv, handler, engine, session) -> bool:
            return True

        self.handler.register_command("context", context_handler)
        with self.assertRaisesRegex(TypeError, "argv, context"):
            self.handler.register_command("legacy", legacy_handler)
        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            self.assertTrue(self.handler.handle("/context value", session))
        self.assertEqual(context_calls[0][0], ["/context", "value"])
        self.assertIs(context_calls[0][1].agent, self.agent)

    def test_thinking_toggle_persists_to_state(self) -> None:
        from enkiball.config import get_show_thinking

        self.assertFalse(self.agent.show_thinking)

        self.handler._thinking()
        self.assertTrue(self.agent.show_thinking)

        state = json.loads((self.agent._state_path).read_text())
        self.assertIs(state.get("show_thinking"), True)
        self.assertTrue(get_show_thinking(self.agent._state_path))

        engine = ChatEngine({}, state_path=self.agent._state_path)
        self.assertTrue(engine.show_thinking)

    def test_command_completions_are_instance_local_and_removed(self) -> None:
        other = CommandHandler(
            Console(file=None, quiet=True), self.agent
        )

        self.handler.register_command("scan", lambda argv, context: True, completions=["fast"])

        self.assertIn("/scan", self.handler.completions)
        self.assertIn("/scan fast", self.handler.completions)
        self.assertNotIn("/scan", other.completions)
        self.assertTrue(self.handler.unregister_command("scan"))
        self.assertNotIn("/scan", self.handler.completions)

    def test_reserved_command_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.handler.register_command("help", lambda argv, context: True)

    def test_session_and_mcp_require_complete_commands(self) -> None:
        output = io.StringIO()
        handler = CommandHandler(
            Console(file=output, force_terminal=False),
            self.agent,
        )

        class NoNestedPrompt:
            def prompt(self, *_args, **_kwargs):
                raise AssertionError("slash commands must not start a nested prompt")

        session = NoNestedPrompt()
        self.assertTrue(handler.handle("/session", session))  # type: ignore[arg-type]
        self.assertTrue(handler.handle("/mcp", session))  # type: ignore[arg-type]

        rendered = output.getvalue()
        self.assertIn("usage: /session", rendered)
        self.assertIn("usage: /mcp", rendered)

    def test_btw_is_a_builtin_and_passes_context_and_stop_callback(self) -> None:
        output = io.StringIO()
        handler = CommandHandler(
            Console(file=output, force_terminal=False),
            self.agent,
            btw_context=lambda: "durable context",
        )
        calls: list[dict[str, object]] = []

        def fake_btw(message: str, **kwargs):
            calls.append({"message": message, **kwargs})
            return type("Result", (), {"content": "answer", "interrupted": False})()

        with patch.object(self.agent, "btw", side_effect=fake_btw):
            handler.set_stop_callback(lambda: False)
            with create_pipe_input() as pipe_input:
                session = PromptSession(input=pipe_input, output=DummyOutput())
                self.assertTrue(handler.handle("/btw inspect status", session))

        self.assertEqual(calls[0]["message"], "inspect status")
        self.assertEqual(calls[0]["system_context"], "durable context")
        self.assertTrue(callable(calls[0]["should_stop"]))
        self.assertIn("answer", output.getvalue())

    def test_revert_uses_agent_api_and_restores_user_content(self) -> None:
        self.agent.load_messages(
            [
                {"role": "system", "content": "system"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect this"},
                        {"type": "input_image", "path": "/tmp/screen.png"},
                    ],
                },
                {"role": "assistant", "content": "done"},
            ]
        )

        with patch.object(self.agent, "revert", wraps=self.agent.revert) as revert:
            with create_pipe_input() as pipe_input:
                session = PromptSession(input=pipe_input, output=DummyOutput())
                self.assertTrue(self.handler.handle("/revert", session))

        revert.assert_called_once_with(1)
        self.assertEqual(self.handler.pending_input, "inspect this")
        self.assertEqual(self.handler.staged_images, ["/tmp/screen.png"])
        self.assertEqual(self.agent.user_turn_count(), 0)

    def test_debug_executes_cell_in_supplied_scope(self) -> None:
        output = io.StringIO()
        scope = {"value": {"count": 1}}
        handler = CommandHandler(
            Console(file=output, force_terminal=False),
            self.agent,
            debug_scope=scope,
        )

        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertTrue(
                    handler.handle(
                        '/debug print("cell output"); value["count"] += 1; value["count"]',
                        session,
                    )
                )

        self.assertEqual(stdout.getvalue(), "cell output\n")
        self.assertEqual(output.getvalue().strip(), "2")
        self.assertEqual(scope["value"]["count"], 2)
        self.assertIs(handler.debug_scope, scope)

    def test_cli_adds_only_agent_and_cli_to_default_debug_scope(self) -> None:
        agent = SimpleNamespace(
            session_store=self.store,
            _config={},
            _mcp=MCPManager({}),
            _config_path=Path("config.json"),
            _state_path=Path("state.json"),
        )

        cli = EnkiballCLI(agent)  # type: ignore[arg-type]

        self.assertEqual(set(cli.debug_scope), {"agent", "cli"})
        self.assertIs(cli.debug_scope["agent"], agent)
        self.assertIs(cli.debug_scope["cli"], cli)


if __name__ == "__main__":
    unittest.main()
