from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

from enkiball.agent import Enkiball
from enkiball.commands import CommandHandler
from enkiball.llm import ChatEngine, TurnUsage
from enkiball.session_store import SessionStore
from enkiball.token_estimation import estimate_tokens
from test_stream_usage import Stream, chunk


class TokenAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.agent = Enkiball.__new__(Enkiball)
        self.agent._engine = ChatEngine({}, model="test", state={})
        self.agent._session_store = SessionStore._from_paths(root / "sessions", root / "exports")
        self.agent._on_tool_event = None
        self.output = io.StringIO()
        self.handler = CommandHandler(Console(file=self.output, width=180, color_system=None), self.agent)

    @staticmethod
    def history():
        return [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer", "_usage": {"prompt_tokens": 100, "completion_tokens": 10}},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "answer", "_usage": {"prompt_tokens": 200, "completion_tokens": 20}},
        ]

    def test_reload_restores_usage_without_double_counting(self):
        self.agent.load_messages(self.history())
        saved = self.agent.save_session("original")
        self.agent._engine = ChatEngine({}, model="test", state={})
        for _ in range(2):
            self.agent.load_session(saved.session_id)
            self.assertEqual(self.agent.token_usage.to_dict(), {
                "prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330,
            })
            self.assertEqual(self.agent.context_tokens, 220)

    def test_cli_new_switch_and_fork_isolate_accounting(self):
        self.agent.load_messages(self.history())
        saved = self.agent.save_session("original")
        self.handler._session_action("new", [])
        self.assertEqual(self.agent.token_usage.total_tokens, 0)
        self.handler._session_action("use", [saved.session_id])
        self.assertEqual(self.agent.token_usage.total_tokens, 330)
        self.handler._session_action("fork", ["1"])
        self.assertEqual(self.agent.token_usage.total_tokens, 110)
        self.assertEqual(self.agent.context_tokens, 110)
        fork_id = self.agent.session_store.current_session_id
        self.agent.load_session(saved.session_id)
        self.assertEqual(self.agent.token_usage.total_tokens, 330)
        self.agent.load_session(fork_id)
        self.assertEqual(self.agent.token_usage.total_tokens, 110)

    def test_revert_reset_and_missing_usage_do_not_retain_previous_session_totals(self):
        self.agent.load_messages(self.history())
        self.agent.revert()
        self.assertEqual(self.agent.token_usage.total_tokens, 110)
        self.assertEqual(self.agent.context_tokens, 110)
        self.agent.reset()
        self.assertEqual(self.agent.token_usage.total_tokens, 0)
        self.agent.load_messages([{"role": "user", "content": "历史文本" * 1000}])
        self.assertEqual(self.agent.token_usage.total_tokens, 0)
        self.assertGreater(self.agent.context_tokens, 4000)

    def test_reset_usage_survives_reload_without_resetting_context(self):
        self.agent.load_messages(self.history())
        before = self.agent.context_tokens
        self.agent.reset_token_usage()
        saved = self.agent.save_session()
        self.agent.load_session(saved.session_id)
        self.assertEqual(self.agent.token_usage.total_tokens, 0)
        self.assertEqual(self.agent.context_tokens, before)
        with patch.object(self.agent._engine, "_client", return_value=self.client()):
            self.agent.chat("next")
        self.assertEqual(self.agent.token_usage.total_tokens, 1010)
        self.agent.load_messages(self.agent.messages)
        self.assertEqual(self.agent.token_usage.total_tokens, 1010)

    @staticmethod
    def client(async_mode=False, events=None):
        if events is None:
            usage = {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}
            events = [chunk({"content": "answer"}, usage), chunk(usage=usage)]
        client = Mock()
        client.chat.completions.create.return_value = Stream(events)
        if async_mode:
            async def create(**kwargs):
                return Stream(events)
            async def close():
                pass
            client.chat.completions.create = create
            client.close = close
        return client

    def test_context_is_latest_request_not_cumulative_in_sync_and_async(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, model="test", state={})
                usage = {"prompt_tokens": 150000, "completion_tokens": 1000, "total_tokens": 151000}
                client = self.client(async_mode, [chunk({"content": "answer"}, usage), chunk(usage=usage)])
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    for _ in range(8):
                        if async_mode:
                            asyncio.run(engine.astream_reply("hi", [], lambda _: None))
                        else:
                            engine.stream_reply("hi", [], lambda _: None)
                self.assertEqual(engine.token_usage.total_tokens, 1208000)
                self.assertEqual(engine.context_tokens, 151000)
                engine.load_messages(engine.export_messages())
                self.assertEqual(engine.token_usage.total_tokens, 1208000)
                self.assertEqual(engine.context_tokens, 151000)
                self.agent._engine = engine
                self.handler._tokens()
                self.assertIn("context≈151,000", self.output.getvalue())
                self.assertIn("total=1,208,000", self.output.getvalue())

    def test_context_calibration_accounts_for_changed_messages_and_tools(self):
        engine = self.agent._engine
        with patch.object(engine, "_client", return_value=self.client()):
            self.agent.chat("hi")
        self.assertEqual(self.agent.context_tokens, 1010)
        engine.messages.append({"role": "user", "content": "x" * 4000})
        self.assertGreater(self.agent.context_tokens, 2010)
        before = self.agent.context_tokens
        engine.set_system_prompt("x" * 8000)
        self.assertGreater(self.agent.context_tokens, before)
        before = self.agent.context_tokens
        engine.register_tool("extra", {"type": "function", "function": {
            "name": "extra", "description": "x" * 4000,
            "parameters": {"type": "object", "properties": {}},
        }}, lambda _: "ok")
        self.assertGreater(self.agent.context_tokens, before)
        engine._model_override = "different"
        self.assertEqual(engine.context_tokens, engine._estimate_context_tokens())

    def test_tool_output_is_counted_without_counting_tool_call_twice(self):
        engine = self.agent._engine
        usage = {"prompt_tokens": 10000, "completion_tokens": 100, "total_tokens": 10100}
        events = [chunk({"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {
            "name": "large", "arguments": "{}",
        }}]}, usage)]
        engine.register_tool("large", {"type": "function", "function": {
            "name": "large", "parameters": {"type": "object", "properties": {}},
        }}, lambda _: "x" * 4000)
        client = self.client(events=events)
        # End after one tool iteration, leaving the tool result in context.
        with patch.object(engine, "_client", return_value=client), patch("enkiball.llm.TOOL_CALL_LIMIT", 1):
            self.agent.chat("hi")
        tail = engine.messages[-2:]
        expected = 10100 + sum(4 + estimate_tokens(msg) for msg in tail)
        self.assertEqual(engine.context_tokens, expected)
        self.assertEqual(engine.token_usage.total_tokens, 10100)

    def test_missing_usage_and_images_use_estimates_without_counting_base64(self):
        engine = self.agent._engine
        engine.load_messages([{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1000000},
        }]}])
        self.assertLess(engine.context_tokens, 10000)
        self.assertGreater(engine.context_tokens, 1024)
        with patch.object(engine, "_client", return_value=self.client(events=[chunk({"content": "text" * 1000})])):
            self.agent.chat("hi")
        self.assertEqual(engine.token_usage.total_tokens, 0)
        self.assertGreater(engine.context_tokens, 2024)

    def test_tool_schema_union_types_can_be_estimated(self):
        engine = self.agent._engine
        engine.register_tool("nullable", {"type": "function", "function": {
            "name": "nullable", "parameters": {"type": "object", "properties": {
                "value": {"type": ["string", "null"]},
            }},
        }}, lambda _: "ok")
        with patch.object(engine, "_client", return_value=self.client()):
            self.agent.chat("hi")
        self.assertEqual(engine.context_tokens, 1010)

    def test_compact_usage_restores_but_does_not_inflate_context(self):
        engine = self.agent._engine
        engine.load_messages(self.history())
        engine._commit_compact_summary("short summary", TurnUsage(100000, 20))
        self.assertEqual(engine.token_usage.total_tokens, 100350)
        self.assertLess(engine.context_tokens, 10000)
        payload = engine._prepare_messages_for_api()
        self.assertEqual(payload[0], {"role": "system", "content": "instructions"})
        self.assertEqual(len(payload), 2)
        self.assertNotIn("_usage", payload[-1])
        engine.auto_compact_enabled = True
        engine.auto_compact_threshold_tokens = 1
        self.assertFalse(engine._should_auto_compact())
        saved = self.agent.save_session()
        before = engine.context_tokens
        self.agent.load_session(saved.session_id)
        self.assertEqual(engine.token_usage.total_tokens, 100350)
        self.assertEqual(engine.context_tokens, before)
        engine._commit_compact_summary("second summary", TurnUsage(300, 10))
        self.assertEqual(len(engine._build_compact_request(None)["messages"]), 3)
        self.assertEqual(engine._prepare_messages_for_api()[0]["content"], "instructions")

    def test_empty_compact_keeps_visible_history_in_sync_and_async(self):
        for async_mode in (False, True):
            engine = ChatEngine({}, model="test", state={})
            engine.load_messages(self.history())
            before = engine.export_messages()
            client = self.client(async_mode, [chunk({"content": "  "})])
            with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "empty compact summary"):
                    if async_mode:
                        asyncio.run(engine.acompact())
                    else:
                        engine.compact()
            self.assertEqual(engine.messages, before)
            self.assertEqual(engine.compact_index, 0)

    def test_compact_threshold_is_independent_of_max_output_tokens(self):
        engine = ChatEngine({"providers": {"p": {"models": {
            "known": {"max_tokens": 100, "context_window": 10000},
            "unknown": {"max_tokens": 500},
        }}}}, model="known", state={"active_provider": "p"})
        self.assertEqual(engine._auto_compact_effective_threshold_tokens(), 7000)
        self.assertNotIn("context_window", engine._build_stream_request({"max_tokens": 100}))
        engine._model_override = "unknown"
        self.assertIsNone(engine.context_window)
        self.assertEqual(engine._auto_compact_effective_threshold_tokens(), 0)
        engine.auto_compact_threshold_tokens = 5000
        self.assertEqual(engine._auto_compact_effective_threshold_tokens(), 5000)


if __name__ == "__main__":
    unittest.main()
