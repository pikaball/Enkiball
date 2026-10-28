from __future__ import annotations

import asyncio
import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from enkiball import Enkiball
from enkiball.llm import ChatEngine
from test_stream_usage import Stream, chunk


class CaptureTests(unittest.TestCase):
    def test_text_thinking_and_tools_keep_arrival_order(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, state={}, model="test")
                snapshots = {False: [], True: []}

                def observe(*_):
                    for thinking, seen in snapshots.items():
                        snapshot = engine.capture(collect_thinking=thinking)
                        self.assertTrue(snapshot.in_progress)
                        seen.append(snapshot)

                def tool(args):
                    self.assertEqual(args, {"x": 1})
                    observe()
                    return "result|"

                engine.register_tool("probe", {
                    "type": "function", "function": {
                        "name": "probe", "parameters": {"type": "object"},
                    },
                }, tool)
                streams = iter([
                    Stream([
                        chunk({"reasoning_content": "think1|"}),
                        chunk({"content": "before|"}),
                        chunk({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                              "function": {"name": "probe", "arguments": '{"x":'}}]}),
                        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}),
                    ]),
                    Stream([
                        chunk({"reasoning_content": "think2|"}),
                        chunk({"content": "after|"}),
                    ]),
                ])

                def create(**kwargs):
                    observe()
                    return next(streams)

                client = Mock()
                client.chat.completions.create = AsyncMock(side_effect=create) if async_mode else Mock(side_effect=create)
                if async_mode:
                    client.close = AsyncMock()
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    reply = engine.astream_reply if async_mode else engine.stream_reply
                    result = reply("hi", [], observe, on_thinking=observe, on_tool_event=observe)
                    if async_mode:
                        result = asyncio.run(result)
                self.assertEqual(result, "after|")
                markers = (
                    '<tool_call_pending name=\'probe\'>{"x":1}</tool_call_pending>\n'
                    "<tool_result name='probe'>\nresult|\n</tool_result>"
                )
                for thinking, seen in snapshots.items():
                    expected = "before|" + markers + "after|"
                    if thinking:
                        expected = "think1|before|" + markers + "think2|after|"
                    final = engine.capture(thinking)
                    self.assertEqual(final.content, expected)
                    self.assertFalse(final.in_progress)
                    self.assertTrue(all(s.in_progress for s in seen))
                    for earlier, later in zip(seen, seen[1:] + [final]):
                        self.assertTrue(later.content.startswith(earlier.content))
                    self.assertEqual(engine.capture(thinking), final)

    def test_new_turn_resets_capture_even_when_initialization_fails(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, state={}, model="test")
                client = Mock()
                create = AsyncMock if async_mode else Mock
                client.chat.completions.create = create(return_value=Stream([chunk({"content": "old"})]))
                if async_mode:
                    client.close = AsyncMock()
                client_method = "_async_client" if async_mode else "_client"
                reply = engine.astream_reply if async_mode else engine.stream_reply

                def run():
                    result = reply("hi", [], lambda _: None)
                    return asyncio.run(result) if async_mode else result

                with patch.object(engine, client_method, return_value=client):
                    run()
                previous = engine.capture()
                with patch.object(engine, client_method, side_effect=ValueError("initialization failed")):
                    with self.assertRaisesRegex(ValueError, "initialization failed"):
                        run()
                self.assertEqual(previous.content, "old")
                self.assertEqual(engine.capture().content, "")
                self.assertFalse(engine.capture().in_progress)
                self.assertTrue(engine.wait(0))


class WaitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.agent = Enkiball(
            config_path=root / "config.json", state_path=root / "state.json",
            session_dir=root / "sessions", cwd=root, skill_dirs=[], auto_start_mcp=False,
        )
        self.addCleanup(self.agent.shutdown)

    def gate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def block(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test gate timed out")

        return entered, release, block

    def test_wait_covers_submitted_job_before_turn_starts(self):
        entered, release, block = self.gate()
        original_chat = self.agent.chat
        client = Mock()
        client.chat.completions.create.return_value = Stream([chunk({"content": "done"})])

        def delayed_chat(*args, **kwargs):
            block()
            return original_chat(*args, **kwargs)

        with patch.object(self.agent, "chat", side_effect=delayed_chat), patch.object(self.agent._engine, "_client", return_value=client):
            future = self.agent.chat_in_background("hi")
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.agent.capture().in_progress)
                self.assertFalse(self.agent.wait(timeout=0.01))
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertTrue(self.agent.wait(timeout=1))
            self.assertEqual(future.result().content, "done")

    def test_wait_includes_initialization_and_client_cleanup(self):
        for compact in (False, True):
            for async_mode in (False, True):
                with self.subTest(compact=compact, async_mode=async_mode):
                    initializing, initialize, block_init = self.gate()
                    closing, close, block_close = self.gate()
                    client = Mock()
                    create = AsyncMock if async_mode else Mock
                    client.chat.completions.create = create(return_value=Stream([chunk({"content": "done"})]))
                    client.close = AsyncMock(side_effect=block_close) if async_mode else Mock(side_effect=block_close)

                    def make_client():
                        block_init()
                        return client

                    def run():
                        if compact:
                            return asyncio.run(self.agent.acompact()) if async_mode else self.agent.compact()
                        return asyncio.run(self.agent.achat("hi")) if async_mode else self.agent.chat("hi")

                    with patch.object(self.agent._engine, "_async_client" if async_mode else "_client", side_effect=make_client), concurrent.futures.ThreadPoolExecutor() as executor:
                        task = executor.submit(run)
                        try:
                            self.assertTrue(initializing.wait(1))
                            self.assertFalse(self.agent.wait(timeout=0))
                            initialize.set()
                            self.assertTrue(closing.wait(1))
                            self.assertFalse(self.agent.wait(timeout=0.01))
                            if not compact:
                                self.assertTrue(self.agent.capture().in_progress)
                        finally:
                            initialize.set()
                            close.set()
                        self.assertTrue(self.agent.wait(timeout=1))
                        task.result(timeout=1)
                    self.assertFalse(self.agent.capture().in_progress)

    def test_wait_reports_completion_without_raising_task_errors(self):
        self.assertTrue(self.agent.wait(timeout=0))
        with patch.object(self.agent._engine, "_client", side_effect=RuntimeError("failed")):
            future = self.agent.chat_in_background("hi")
            self.assertTrue(self.agent.wait(timeout=1))
            with self.assertRaisesRegex(RuntimeError, "failed"):
                future.result()

    def test_cancelled_queued_job_does_not_hide_other_pending_work(self):
        entered, release, block = self.gate()
        with patch.object(self.agent, "chat", side_effect=block):
            first = self.agent.chat_in_background("one")
            try:
                self.assertTrue(entered.wait(1))
                second = self.agent.chat_in_background("two")
                self.assertTrue(second.cancel())
                self.assertFalse(self.agent.wait(timeout=0.01))
            finally:
                release.set()
            self.assertTrue(self.agent.wait(timeout=1))
            self.assertTrue(first.done())
            self.assertTrue(second.cancelled())
