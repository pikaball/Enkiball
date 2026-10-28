from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from pathlib import Path
from unittest.mock import Mock, patch

from enkiball.llm import ChatEngine
from enkiball.mcp import MCPManager
from enkiball.mcp_client import SSEMCPClient, StdioMCPClient, StreamableHTTPMCPClient
from enkiball.sandbox import TurnInterrupted
from test_stream_usage import Stream, chunk


STDIO_SERVER = r'''
import json, subprocess, sys, time
framed = sys.argv[1] == "content-length"
def send(msg):
    data = json.dumps(msg).encode()
    if framed:
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
        sys.stdout.buffer.flush()
        # Exercise fragmented pipe reads, including during initialize.
        sys.stdout.buffer.write(data[:5])
        sys.stdout.buffer.flush()
        time.sleep(0.01)
        sys.stdout.buffer.write(data[5:])
    else:
        sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    if framed:
        length = int(line.split(b":", 1)[1])
        sys.stdin.buffer.readline()
        msg = json.loads(sys.stdin.buffer.read(length))
    else:
        msg = json.loads(line)
    method = msg["method"]
    if method == "notifications/cancelled":
        send({"method": "test/cancelled", "params": msg["params"]})
        send({"id": msg["params"]["requestId"], "result": "late"})
    if "id" not in msg:
        continue
    if method == "tools/call":
        name = msg["params"]["name"]
        send({"method": "test/received", "params": {"requestId": msg["id"]}})
        if name == "slow":
            continue
        if name == "exit":
            break
        if name == "block_stdin":
            time.sleep(10)
            break
        result = {"content": [{"type": "text", "text": "ok"}]}
        if name == "spawn":
            child = subprocess.Popen([sys.executable, "-u", "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"],
                stdout=subprocess.PIPE)
            child.stdout.readline()
            result["child_pid"] = child.pid
    else:
        result = {"tools": [], "resources": [], "prompts": []}
    send({"id": msg["id"], "result": result})
    if method == "tools/call" and name == "last":
        break
'''


class StdioInterruptTests(unittest.TestCase):
    def make_client(self, transport="jsonl"):
        client = StdioMCPClient("test", sys.executable, ["-u", "-c", STDIO_SERVER, transport],
                                transport=transport)
        received = threading.Event()
        cancelled = threading.Event()
        notifications = []
        handle_message = client._handle_message

        def handle(msg):
            if msg.get("method") == "test/received":
                received.set()
            if msg.get("method") == "test/cancelled":
                notifications.append(msg["params"])
                cancelled.set()
            handle_message(msg)

        client._handle_message = handle
        self.addCleanup(client.close)
        client.start()
        return client, received, cancelled, notifications

    def test_cancel_response_wait_and_reuse_connection(self):
        for transport in ("jsonl", "content-length"):
            with self.subTest(transport=transport):
                client, ready, cancelled, notices = self.make_client(transport)
                event = threading.Event()
                worker = threading.Thread(target=lambda: (ready.wait(3), event.set()))
                worker.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(TurnInterrupted):
                        client.call_tool("slow", {}, cancel_event=event)
                finally:
                    worker.join(4)
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(cancelled.wait(1))
                self.assertEqual(notices[0]["requestId"], client._request_id)
                self.assertEqual(client._pending_requests, {})
                self.assertEqual(client.call_tool("echo", {})["content"][0]["text"], "ok")
                client.close()
                self.assertFalse(client.thread.is_alive())
                self.assertFalse(client.stderr_thread.is_alive())
                self.assertIsNotNone(client.process.poll())

    def test_pre_cancelled_call_is_not_sent(self):
        client, ready, cancelled, _ = self.make_client()
        event = threading.Event()
        event.set()
        with self.assertRaises(TurnInterrupted):
            client.call_tool("echo", {}, cancel_event=event)
        self.assertFalse(ready.is_set())
        self.assertFalse(cancelled.is_set())

    def test_disconnect_wakes_pending_request(self):
        client, _, _, _ = self.make_client()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "connection closed"):
            client.call_tool("exit", {})
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(client._pending_requests, {})

    def test_response_immediately_followed_by_eof_is_not_lost(self):
        client, _, _, _ = self.make_client()
        self.assertEqual(client.call_tool("last", {})["content"][0]["text"], "ok")

    def test_close_wakes_pending_request(self):
        client, ready, _, _ = self.make_client()
        worker = threading.Thread(target=lambda: (ready.wait(3), client.close()))
        worker.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "connection closed"):
                client.call_tool("slow", {})
        finally:
            worker.join(4)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(client._pending_requests, {})

    def test_close_kills_descendants_that_ignore_termination(self):
        client, _, _, _ = self.make_client()
        child_pid = client.call_tool("spawn", {})["child_pid"]
        try:
            client.close()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                except FileNotFoundError:
                    break
                if state == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("MCP child process survived close()")
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_cancel_blocked_pipe_retires_incomplete_frame(self):
        client, ready, _, _ = self.make_client()
        # This request makes the server stop reading; a later large request fills the pipe.
        client._send_payload({"id": 999, "method": "tools/call", "params": {"name": "block_stdin"}})
        self.assertTrue(ready.wait(1))
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TurnInterrupted):
                client.call_tool("echo", {"data": "x" * 1_000_000}, cancel_event=event)
        finally:
            timer.cancel()
            timer.join()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(client.running)
        self.assertIsNotNone(client.process.poll())
        self.assertEqual(client._pending_requests, {})

    def test_engine_sync_and_async_interrupt_preserve_tool_history(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                client, ready, cancelled, _ = self.make_client()
                manager = MCPManager({})
                manager.clients["test"] = client
                engine = ChatEngine({}, state={}, model="test", mcp_manager=manager)
                events = [chunk({"content": "partial", "tool_calls": [
                    {"index": i, "id": f"call-{i}", "type": "function",
                     "function": {"name": "mcp__test__slow", "arguments": "{}"}}
                    for i in range(2)
                ]})]
                llm = Mock()
                llm.chat.completions.create.return_value = Stream(events)
                stop = threading.Event()
                cancel = stop.set if async_mode else engine.interrupt
                worker = threading.Thread(target=lambda: (ready.wait(3), cancel()))
                worker.start()
                if async_mode:
                    async def create(**kwargs):
                        return Stream(events)
                    async def close():
                        pass
                    llm.chat.completions.create = create
                    llm.close = close
                try:
                    with patch.object(engine, "_async_client" if async_mode else "_client", return_value=llm):
                        reply = engine.astream_reply if async_mode else engine.stream_reply
                        result = reply("test", [], lambda _: None, should_stop=stop.is_set)
                        if async_mode:
                            result = asyncio.run(result)
                finally:
                    worker.join(4)
                self.assertEqual(result, "partial")
                self.assertTrue(engine.interrupted())
                self.assertTrue(engine.wait(0))
                self.assertFalse(engine.capture().in_progress)
                self.assertTrue(cancelled.wait(1))
                results = [msg for msg in engine.messages if msg["role"] == "tool"]
                self.assertEqual([msg["tool_call_id"] for msg in results], ["call-0", "call-1"])
                self.assertTrue(all("interrupted" in msg["content"] for msg in results))
                self.assertEqual(client._pending_requests, {})
                client.close()

    def test_async_task_cancel_waits_for_mcp_cleanup(self):
        client, ready, cancelled, _ = self.make_client()
        manager = MCPManager({})
        manager.clients["test"] = client
        engine = ChatEngine({}, mcp_manager=manager)
        turn = engine._begin_turn()

        async def run():
            task = asyncio.create_task(engine._adispatch_tool("mcp__test__slow", {}, turn))
            self.assertTrue(await asyncio.to_thread(ready.wait, 3))
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertLess(time.monotonic() - started, 1)

        try:
            asyncio.run(run())
        finally:
            engine._end_turn(turn)
        self.assertTrue(cancelled.wait(1))
        self.assertEqual(client._pending_requests, {})


class HTTPMCPInterruptTests(unittest.TestCase):
    def setUp(self):
        self.ready = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.events = Queue()
        self.stage = "headers"
        self.requests = []
        self.ignore_cancel = False
        self.initial_threads = {t.ident for t in threading.enumerate() if t.name == "enkiball-http"}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.requests.append((self.path, None, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                # The POST endpoint deliberately arrives after the GET headers.
                time.sleep(0.1)
                try:
                    self.wfile.write(b"event: endpoint\ndata: /messages\n\n")
                    self.wfile.flush()
                    while not test.release.is_set():
                        try:
                            msg = test.events.get(timeout=0.05)
                        except Empty:
                            continue
                        self.wfile.write(("data: " + json.dumps(msg) + "\n\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append((self.path, msg, dict(self.headers)))
                try:
                    if msg["method"] == "notifications/cancelled":
                        test.cancelled.set()
                        if test.ignore_cancel:
                            test.release.wait(5)
                    if "id" not in msg:
                        self.send_response(202)
                        self.end_headers()
                        return
                    slow = msg["method"] == "tools/call" and msg["params"]["name"] == "slow"
                    if slow and test.stage == "headers":
                        test.ready.set()
                        test.release.wait(5)
                    legacy = self.path == "/messages"
                    self.send_response(202 if legacy else 200)
                    self.send_header("Content-Type", "text/event-stream" if test.stage == "body" else "application/json")
                    self.send_header("Mcp-Session-Id", "test-session")
                    self.end_headers()
                    if slow and test.stage == "body":
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        test.ready.set()
                        test.release.wait(5)
                    if slow and test.stage == "queue":
                        test.ready.set()
                        return
                    result = {"tools": [], "resources": [], "prompts": [], "content": [{"type": "text", "text": "ok"}]}
                    response = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                    if legacy:
                        test.events.put(response)
                    else:
                        data = json.dumps(response)
                        if test.stage == "body":
                            data = "data: " + data + "\n\n"
                        self.wfile.write(data.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(2)
        self.assertEqual({t.ident for t in threading.enumerate() if t.name == "enkiball-http"}, self.initial_threads)

    def run_interrupted(self, legacy=False):
        url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        manager = MCPManager({"mcp": {"servers": {"test": {
            "url": url, "type": "sse" if legacy else "streamable-http",
            "headers": {"Authorization": "Bearer test"},
        }}}})
        ok, message = manager.start("test")
        self.assertTrue(ok, message)
        client = manager.clients["test"]
        self.addCleanup(client.close)
        event = threading.Event()
        cancelled_at = []

        def stop():
            if self.ready.wait(3):
                cancelled_at.append(time.monotonic())
                event.set()

        worker = threading.Thread(target=stop)
        worker.start()
        try:
            with self.assertRaises(TurnInterrupted):
                client.call_tool("slow", {}, cancel_event=event)
        finally:
            worker.join(4)
        self.assertTrue(cancelled_at)
        self.assertLess(time.monotonic() - cancelled_at[0], 1)
        self.assertTrue(self.cancelled.wait(1))
        self.assertEqual(client._pending_requests, {})
        calls = [msg for _, msg, _ in self.requests if msg and msg["method"] == "tools/call"]
        cancels = [(msg, headers) for _, msg, headers in self.requests if msg and msg["method"] == "notifications/cancelled"]
        self.assertEqual(cancels[0][0]["params"]["requestId"], calls[0]["id"])
        if not legacy:
            self.assertEqual(cancels[0][1]["Mcp-Session-Id"], "test-session")
        self.assertTrue(all(headers.get("Authorization") == "Bearer test" for _, _, headers in self.requests))
        self.assertEqual(client.call_tool("echo", {})["content"][0]["text"], "ok")
        client.close()
        if legacy:
            self.assertFalse(client.thread.is_alive())
            self.assertTrue(all(path == "/messages" for path, msg, _ in self.requests if msg))

    def test_streamable_http_before_headers(self):
        self.run_interrupted()

    def test_streamable_http_during_body(self):
        self.stage = "body"
        self.run_interrupted()

    def test_unresponsive_cancellation_post_is_bounded(self):
        self.ignore_cancel = True
        self.run_interrupted()

    def test_sse_before_post_headers(self):
        self.run_interrupted(legacy=True)

    def test_sse_during_post_body(self):
        self.stage = "body"
        self.run_interrupted(legacy=True)

    def test_sse_waiting_for_event(self):
        self.stage = "queue"
        self.run_interrupted(legacy=True)

    def test_close_aborts_inflight_http_requests(self):
        for cls in (SSEMCPClient, StreamableHTTPMCPClient):
            with self.subTest(client=cls.__name__):
                self.ready.clear()
                client = cls("test", f"http://127.0.0.1:{self.server.server_port}/mcp")
                self.addCleanup(client.close)
                client.start()
                worker = threading.Thread(target=lambda: (self.ready.wait(3), client.close()))
                worker.start()
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(RuntimeError, "connection closed"):
                        client.call_tool("slow", {})
                finally:
                    worker.join(4)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(client._pending_requests, {})

    def test_cancellation_does_not_block_other_calls(self):
        client = StreamableHTTPMCPClient("test", f"http://127.0.0.1:{self.server.server_port}/mcp")
        self.addCleanup(client.close)
        client.start()
        event = threading.Event()
        errors = []

        def slow():
            try:
                client.call_tool("slow", {}, cancel_event=event)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=slow)
        worker.start()
        try:
            self.assertTrue(self.ready.wait(3))
            self.assertEqual(client.call_tool("echo", {})["content"][0]["text"], "ok")
        finally:
            event.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TurnInterrupted)


if __name__ == "__main__":
    unittest.main()
