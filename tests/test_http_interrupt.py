from __future__ import annotations

import asyncio
import json
import os
import pty
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from enkiball.cli import _StreamInputMonitor
from enkiball.llm import ChatEngine
from enkiball.sandbox import TurnInterrupted


class HTTPInterruptTests(unittest.TestCase):
    def setUp(self):
        self.ready = threading.Event()
        self.release = threading.Event()
        self.stage = "headers"
        self.requests = 0
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                test.requests += 1
                try:
                    if test.stage == "error":
                        self.send_response(503)
                        self.end_headers()
                        return
                    if test.stage == "headers":
                        test.ready.set()
                        test.release.wait(5)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if test.stage == "body":
                        payload = {"id": "test", "object": "chat.completion.chunk",
                                   "created": 0, "model": "test", "choices": [{
                                       "index": 0, "delta": {"content": "partial"}}]}
                        self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                        self.wfile.flush()
                        test.ready.set()
                        test.release.wait(5)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.engine = ChatEngine({"providers": {"local": {
            "api_key": "test-key",
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
        }}}, state={}, model="test")
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def run_interrupted(self, async_mode=False, compact=False, explicit=False, esc=False):
        stop = threading.Event()
        tokens = []
        retries = []
        cancelled_at = []
        initial_threads = {t.ident for t in threading.enumerate() if t.name == "enkiball-http"}
        master = slave = None
        if esc:
            master, slave = pty.openpty()

        def cancel():
            if not self.ready.wait(3):
                self.release.set()
                return
            if self.stage == "body":
                # Allow the first SSE event to reach the callback before stalling.
                deadline = time.monotonic() + 1
                while not tokens and time.monotonic() < deadline:
                    time.sleep(0.01)
            cancelled_at.append(time.monotonic())
            if esc:
                os.write(master, b"\x1b")
            elif explicit:
                self.engine.interrupt()
            else:
                stop.set()

        worker = threading.Thread(target=cancel)
        worker.start()
        try:
            if esc:
                with os.fdopen(os.dup(slave), "r") as terminal, patch("sys.stdin", terminal):
                    with _StreamInputMonitor() as monitor:
                        result = self.engine.stream_reply(
                            "hello", [], tokens.append, should_stop=monitor.should_stop,
                            on_retry=retries.append,
                        )
            elif compact:
                original = self.engine.export_messages()
                with self.assertRaises(TurnInterrupted):
                    if async_mode:
                        asyncio.run(self.engine.acompact(on_token=tokens.append, on_retry=retries.append))
                    else:
                        self.engine.compact(on_token=tokens.append, on_retry=retries.append,
                                            should_stop=stop.is_set)
                self.assertEqual(self.engine.messages, original)
                result = ""
            else:
                kwargs = {"should_stop": None if explicit else stop.is_set,
                          "on_retry": retries.append}
                if async_mode:
                    result = asyncio.run(self.engine.astream_reply("hello", [], tokens.append, **kwargs))
                else:
                    result = self.engine.stream_reply("hello", [], tokens.append, **kwargs)
            finished_at = time.monotonic()
        finally:
            self.release.set()
            worker.join(timeout=4)
            if master is not None:
                os.close(master)
                os.close(slave)
        self.assertTrue(cancelled_at, "server never received the request")
        self.assertLess(finished_at - cancelled_at[0], 1.0)
        self.assertEqual(retries, [])
        self.assertEqual(self.requests, 1)
        self.assertTrue(self.engine.interrupted())
        self.assertFalse(self.engine.capture().in_progress)
        self.assertEqual({t.ident for t in threading.enumerate() if t.name == "enkiball-http"}, initial_threads)
        if self.stage == "body" and not compact:
            self.assertEqual(result, "partial")
            self.assertEqual(self.engine.messages[-1]["content"], "partial")
        turn = self.engine._begin_turn()
        self.engine._end_turn(turn)

    def test_cli_escape_before_response_headers(self):
        self.run_interrupted(esc=True)

    def test_sync_stop_between_chunks_keeps_partial_output(self):
        self.stage = "body"
        self.run_interrupted()

    def test_sync_explicit_interrupt_before_response(self):
        self.run_interrupted(explicit=True)

    def test_async_stop_before_response(self):
        self.run_interrupted(async_mode=True)

    def test_async_explicit_interrupt_between_chunks(self):
        self.stage = "body"
        self.run_interrupted(async_mode=True, explicit=True)

    def test_compact_stop_before_response(self):
        self.run_interrupted(compact=True)

    def test_async_compact_interrupt_keeps_history(self):
        self.stage = "body"
        self.run_interrupted(async_mode=True, compact=True, explicit=True)

    def test_stop_during_retry_returns_without_another_request(self):
        self.stage = "error"
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                stop = threading.Event()
                before = self.requests
                started = time.monotonic()
                kwargs = {"should_stop": stop.is_set, "on_retry": lambda _: stop.set()}
                if async_mode:
                    result = asyncio.run(self.engine.astream_reply("hello", [], lambda _: None, **kwargs))
                else:
                    result = self.engine.stream_reply("hello", [], lambda _: None, **kwargs)
                self.assertEqual(result, "")
                self.assertTrue(self.engine.interrupted())
                self.assertEqual(self.requests, before + 1)
                self.assertLess(time.monotonic() - started, 2)

    def test_async_task_cancellation_closes_client(self):
        async def run():
            task = asyncio.create_task(self.engine.astream_reply("hello", [], lambda _: None))
            deadline = time.monotonic() + 3
            while not self.ready.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(self.ready.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(self.engine.interrupted())
            self.assertFalse(self.engine.capture().in_progress)
        asyncio.run(run())

    def test_normal_requests_still_work_after_an_interruption(self):
        self.run_interrupted()
        self.stage = "body"
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                if async_mode:
                    result = asyncio.run(self.engine.astream_reply("again", [], lambda _: None))
                else:
                    result = self.engine.stream_reply("again", [], lambda _: None)
                self.assertEqual(result, "partial")
                self.assertFalse(self.engine.interrupted())
