from __future__ import annotations

import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

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

    def test_schema_only_extra_tools_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "use register_tool"):
            self.engine.set_extra_tools([self.schema])


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
        self.handler = CommandHandler(
            Console(file=None, quiet=True),
            {},
            MCPManager({}),
            self.store,
        )

    def test_context_handler_and_legacy_handler_are_supported(self) -> None:
        context_calls: list[tuple[list[str], CommandContext]] = []
        legacy_calls: list[list[str]] = []

        def context_handler(argv: list[str], context: CommandContext) -> bool:
            context_calls.append((argv, context))
            return True

        def legacy_handler(argv, handler, engine, session) -> bool:
            legacy_calls.append(argv)
            return True

        agent = object()
        self.handler.agent = agent  # type: ignore[assignment]
        self.handler.register_command("context", context_handler)
        self.handler.register_command("legacy", legacy_handler)
        engine = ChatEngine({})
        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            self.assertTrue(self.handler.handle("/context value", engine, session))
            self.assertTrue(self.handler.handle("/legacy value", engine, session))
        self.assertEqual(context_calls[0][0], ["/context", "value"])
        self.assertIs(context_calls[0][1].agent, agent)
        self.assertEqual(legacy_calls, [["/legacy", "value"]])

    def test_command_completions_are_instance_local_and_removed(self) -> None:
        other = CommandHandler(
            Console(file=None, quiet=True), {}, MCPManager({}), self.store
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


if __name__ == "__main__":
    unittest.main()
