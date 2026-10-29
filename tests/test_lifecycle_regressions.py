from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console

from enkiball import Enkiball, EnkiballCLI
from enkiball.llm import ChatEngine
from enkiball.sandbox import Sandbox, TurnInterrupted
from test_stream_usage import Stream, chunk


def make_agent():
    agent = Enkiball.__new__(Enkiball)
    agent._engine = ChatEngine({}, state={}, model="test")
    agent._on_tool_event = None
    return agent


class TemporaryTurnTests(unittest.TestCase):
    def test_failed_btw_restores_history_and_releases_ownership(self):
        agent = make_agent()
        before = agent.messages
        client = Mock()
        client.chat.completions.create.side_effect = ValueError("bad request")
        with patch.object(agent._engine, "_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "bad request"):
                agent.btw("temporary question")
        self.assertEqual(agent.messages, before)
        self.assertTrue(agent._engine.wait(0))

    def test_rejected_btw_does_not_overwrite_the_running_turn(self):
        agent = make_agent()
        engine = agent._engine
        active = engine._begin_turn()
        self.addCleanup(engine._end_turn, active)
        begin = engine._begin_turn
        incoming = {"role": "assistant", "content": "concurrent progress"}

        def interleaved_begin():
            # Simulate the active worker progressing just before lock acquisition.
            engine.messages.append(incoming)
            return begin()

        with patch.object(engine, "_begin_turn", side_effect=interleaved_begin):
            with self.assertRaisesRegex(RuntimeError, "another turn"):
                agent.btw("temporary question")
        self.assertIn(incoming, engine.messages)

    def test_btw_holds_turn_ownership_until_history_is_restored(self):
        agent = make_agent()
        engine = agent._engine
        before = engine.export_messages()
        client = Mock()
        client.chat.completions.create.return_value = Stream([chunk({"content": "answer"})])
        restore = engine.load_messages

        def checked_restore(messages):
            self.assertFalse(engine.wait(0), "turn released before restoring history")
            restore(messages)

        with patch.object(engine, "_client", return_value=client), patch.object(engine, "load_messages", side_effect=checked_restore):
            self.assertEqual(agent.btw("temporary question").content, "answer")
        self.assertEqual(engine.messages, before)
        self.assertTrue(engine.wait(0))


class AsyncStreamCleanupTests(unittest.TestCase):
    def test_astream_close_preserves_partial_history_and_closes_client(self):
        agent = make_agent()

        class WaitingStream(Stream):
            async def __aiter__(self):
                yield chunk({"content": "partial"})
                while not agent._engine.interrupted():
                    await asyncio.sleep(0.001)
                raise TurnInterrupted

        async def run():
            client = Mock()
            client.chat.completions.create = AsyncMock(return_value=WaitingStream([]))
            client.close = AsyncMock()
            with patch.object(agent._engine, "_async_client", return_value=client):
                stream = agent.astream("hi")
                self.assertEqual(await anext(stream), "partial")
                await asyncio.wait_for(stream.aclose(), 1)
            client.close.assert_awaited_once()
            self.assertEqual(agent.messages[-1]["content"], "partial")
            self.assertTrue(agent.interrupted)
            self.assertTrue(agent._engine.wait(0))

        asyncio.run(run())

    def test_closing_astream_waits_for_its_worker(self):
        agent = make_agent()

        async def run():
            released = asyncio.Event()
            stopped = asyncio.Event()

            async def reply(**kwargs):
                turn = agent._engine._begin_turn()
                try:
                    await kwargs["on_token"]("first")
                    while not released.is_set() and not (kwargs.get("should_stop") and kwargs["should_stop"]()):
                        await asyncio.sleep(0.001)
                    return "first"
                finally:
                    stopped.set()
                    agent._engine._end_turn(turn)

            with patch.object(agent._engine, "astream_reply", side_effect=reply):
                stream = agent.astream("hi")
                try:
                    self.assertEqual(await anext(stream), "first")
                    await stream.aclose()
                    self.assertTrue(stopped.is_set(), "closed stream left an active request")
                    self.assertTrue(agent._engine.wait(0))
                finally:
                    released.set()
                    await stream.aclose()
                    await asyncio.sleep(0.01)

        asyncio.run(run())

    def test_abandoned_pending_stream_does_not_interrupt_a_different_turn(self):
        agent = make_agent()

        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()
            other = agent._engine._begin_turn()

            async def pending_reply(**kwargs):
                entered.set()
                while not release.is_set() and not (kwargs.get("should_stop") and kwargs["should_stop"]()):
                    await asyncio.sleep(0.001)
                return ""

            with patch.object(agent._engine, "astream_reply", side_effect=pending_reply):
                stream = agent.astream_events("hi")
                consumer = asyncio.create_task(anext(stream))
                try:
                    await entered.wait()
                    consumer.cancel()
                    # Bound old cleanup, which waits without signalling its own worker.
                    asyncio.get_running_loop().call_later(0.05, release.set)
                    with self.assertRaises(asyncio.CancelledError):
                        await consumer
                    self.assertFalse(other.interrupted)
                finally:
                    release.set()
                    await stream.aclose()
                    agent._engine._end_turn(other)

        asyncio.run(run())


class CommandProcessTests(unittest.TestCase):
    def test_timeout_includes_descendants_holding_output_after_shell_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Sandbox(cwd=directory, sandbox=False)
            with self.assertRaises(subprocess.TimeoutExpired):
                sandbox.run("sleep 2 &", timeout_sec=1)

    def test_cancel_includes_descendants_holding_output_after_shell_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Sandbox(cwd=directory, sandbox=False)
            cancelled = threading.Event()
            timer = threading.Timer(0.2, cancelled.set)
            timer.start()
            try:
                with self.assertRaises(TurnInterrupted):
                    sandbox.run("sleep 1 &", cancel_event=cancelled)
            finally:
                timer.cancel()
                timer.join()

    def test_pre_cancelled_command_does_not_start(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("enkiball.sandbox.subprocess.Popen", side_effect=AssertionError("cancelled command was started")) as popen:
            with self.assertRaises(TurnInterrupted):
                Sandbox(sandbox=False).run("true", cancel_event=cancelled)
            popen.assert_not_called()


class CancellationBoundaryTests(unittest.TestCase):
    def test_pre_cancelled_compact_does_not_request_a_client(self):
        for async_mode in (False, True):
            engine = ChatEngine({}, state={}, model="test")
            with patch.object(engine, "_async_client" if async_mode else "_client") as client:
                with self.assertRaises(TurnInterrupted):
                    if async_mode:
                        asyncio.run(engine.acompact(should_stop=lambda: True))
                    else:
                        engine.compact(should_stop=lambda: True)
                client.assert_not_called()
            self.assertTrue(engine.wait(0))

    def test_compact_cancelled_by_last_chunk_does_not_replace_history(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, state={}, model="test")
                before = engine.export_messages()
                client = Mock()
                client.chat.completions.create = (AsyncMock if async_mode else Mock)(
                    return_value=Stream([chunk({"content": "summary"})])
                )
                if async_mode:
                    client.close = AsyncMock()
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    with self.assertRaises(TurnInterrupted):
                        if async_mode:
                            asyncio.run(engine.acompact(on_token=lambda _: engine.interrupt()))
                        else:
                            engine.compact(on_token=lambda _: engine.interrupt())
                self.assertEqual(engine.messages, before)

    def test_custom_tool_can_interrupt_the_turn(self):
        engine = ChatEngine({}, state={}, model="test")
        engine.register_tool("stop", {"type": "function", "function": {
            "name": "stop", "parameters": {"type": "object"},
        }}, Mock(side_effect=TurnInterrupted))
        with self.assertRaises(TurnInterrupted):
            engine._dispatch_tool("stop", {})

    def test_tool_cancellation_stops_followup_requests_and_completes_tool_history(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, state={}, model="test")
                engine.register_tool("stop", {"type": "function", "function": {
                    "name": "stop", "parameters": {"type": "object"},
                }}, Mock(side_effect=TurnInterrupted))
                client = Mock()
                client.chat.completions.create = (AsyncMock if async_mode else Mock)(return_value=Stream([
                    chunk({"tool_calls": [{"index": i, "id": f"call-{i}", "type": "function",
                          "function": {"name": "stop", "arguments": "{}"}} for i in range(2)]}),
                ]))
                if async_mode:
                    client.close = AsyncMock()
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    if async_mode:
                        asyncio.run(engine.astream_reply("hi", [], lambda _: None))
                    else:
                        engine.stream_reply("hi", [], lambda _: None)
                self.assertTrue(engine.interrupted())
                self.assertEqual([msg.get("tool_call_id") for msg in engine.messages[-2:]], ["call-0", "call-1"])
                client.chat.completions.create.assert_called_once()


class CliFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.agent = Enkiball(
            config_path=root / "config.json", state_path=root / "state.json",
            session_dir=root / "sessions", cwd=root, skill_dirs=[], auto_start_mcp=False,
        )
        self.addCleanup(self.agent.shutdown)
        self.agent.load_messages([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ])
        self.output = io.StringIO()
        self.cli = EnkiballCLI(self.agent, console=Console(file=self.output), history_path=root / "history")

    def test_failed_slash_command_returns_to_prompt(self):
        prompt = Mock()
        prompt.prompt.side_effect = ["/compact", "/exit"]
        with patch("enkiball.cli.PromptSession", return_value=prompt), patch.object(
            self.agent, "compact", side_effect=RuntimeError("provider unavailable")
        ):
            self.cli.run()
        self.assertEqual(prompt.prompt.call_count, 2)
        self.assertIn("provider unavailable", self.output.getvalue())

    def test_unexpected_cli_error_still_shuts_down_resources(self):
        prompt = Mock()
        prompt.prompt.side_effect = RuntimeError("input failed")
        with patch("enkiball.cli.PromptSession", return_value=prompt), patch.object(self.agent, "shutdown") as shutdown:
            with self.assertRaisesRegex(RuntimeError, "input failed"):
                self.cli.run()
            shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
