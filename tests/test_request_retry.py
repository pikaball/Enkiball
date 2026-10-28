from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from enkiball.llm import ChatEngine
from test_stream_usage import Stream, chunk


def status_error(code):
    response = httpx.Response(code, request=httpx.Request("POST", "http://test/v1/chat/completions"))
    return APIStatusError("request failed", response=response, body=None)


class BrokenStream(Stream):
    def __iter__(self):
        yield from self.events
        raise httpx.ReadError("stream disconnected")

    async def __aiter__(self):
        for event in self:
            yield event


class RequestRetryTests(unittest.TestCase):
    @contextmanager
    def request(self, async_mode, effects, *, compact=False, **callbacks):
        engine = ChatEngine({}, state={}, model="test")
        client = Mock()
        client.chat.completions.create = (AsyncMock if async_mode else Mock)(side_effect=effects)
        if async_mode:
            client.close = AsyncMock()

        def run():
            if compact:
                call = engine.acompact if async_mode else engine.compact
                result = call(**callbacks)
            else:
                call = engine.astream_reply if async_mode else engine.stream_reply
                result = call("hi", [], **{"on_token": lambda _: None, **callbacks})
            return asyncio.run(result) if async_mode else result

        with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client), patch.object(engine, "_await_retry" if async_mode else "_wait_for_retry", return_value=True):
            yield engine, client, run
        client.close.assert_called_once()
        self.assertTrue(engine.wait(timeout=0))
        self.assertFalse(engine.capture().in_progress)

    def test_transient_failures_retry_before_output(self):
        request = httpx.Request("POST", "http://test")
        errors = [
            APIConnectionError(request=request), APITimeoutError(request=request),
            httpx.ReadError("read"), httpx.ReadTimeout("timeout"),
            httpx.RemoteProtocolError("disconnected"),
            *(status_error(code) for code in (408, 409, 429, 500, 503, 599)),
        ]
        for async_mode in (False, True):
            for compact in (False, True):
                for error in errors:
                    with self.subTest(async_mode=async_mode, compact=compact, error=repr(error)):
                        retries = []
                        effects = [error, Stream([chunk({"content": "ok"})])]
                        with self.request(async_mode, effects, compact=compact, on_retry=retries.append) as (_, client, run):
                            self.assertEqual(run(), "ok")
                            self.assertEqual(client.chat.completions.create.call_count, 2)
                            self.assertEqual(len(retries), 1)

    def test_permanent_and_application_errors_do_not_retry(self):
        errors = [ValueError("invalid"), RuntimeError("bug"),
                  *(status_error(code) for code in (400, 401, 403, 404, 422))]
        for async_mode in (False, True):
            for compact in (False, True):
                for error in errors:
                    with self.subTest(async_mode=async_mode, compact=compact, error=repr(error)):
                        retries = []
                        with self.request(async_mode, [error], compact=compact, on_retry=retries.append) as (_, client, run):
                            with self.assertRaises(type(error)) as raised:
                                run()
                            self.assertIs(raised.exception, error)
                            self.assertEqual(client.chat.completions.create.call_count, 1)
                            self.assertEqual(retries, [])

    def test_disconnect_after_text_or_thinking_does_not_replay_output(self):
        for async_mode in (False, True):
            for kind in ("content", "reasoning_content", "reasoning"):
                with self.subTest(async_mode=async_mode, kind=kind):
                    emitted, retries = [], []
                    stream = BrokenStream([chunk({kind: "partial"})])
                    with self.request(async_mode, [stream], on_token=emitted.append,
                                      on_thinking=emitted.append, on_retry=retries.append) as (engine, client, run):
                        with self.assertRaises(httpx.ReadError):
                            run()
                        self.assertEqual(emitted, ["partial"])
                        self.assertEqual(retries, [])
                        self.assertEqual(client.chat.completions.create.call_count, 1)
                        self.assertEqual(engine.capture(True).content, "partial")

    def test_partial_compact_is_not_retried_or_committed(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                emitted = []
                with self.request(async_mode, [BrokenStream([chunk({"content": "partial"})])],
                                  compact=True, on_token=emitted.append) as (engine, client, run):
                    original = engine.export_messages()
                    with self.assertRaises(httpx.ReadError):
                        run()
                    self.assertEqual(emitted, ["partial"])
                    self.assertEqual(engine.messages, original)
                    self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_callback_error_is_not_treated_as_a_connection_failure(self):
        for async_mode in (False, True):
            for kind in ("content", "reasoning_content"):
                with self.subTest(async_mode=async_mode, kind=kind):
                    callback = Mock(side_effect=httpx.ReadError("callback failed"))
                    with self.request(async_mode, [Stream([chunk({kind: "partial"})])],
                                      on_token=callback, on_thinking=callback) as (_, client, run):
                        with self.assertRaisesRegex(httpx.ReadError, "callback failed"):
                            run()
                        callback.assert_called_once_with("partial")
                        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_retry_state_resets_for_next_request_after_tool_execution(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                effects = [
                    Stream([chunk({"content": "before"}), chunk({"tool_calls": [{
                        "index": 0, "id": "call-1", "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }]})]),
                    httpx.ReadError("second request failed"),
                    Stream([chunk({"content": "after"})]),
                ]
                with self.request(async_mode, effects) as (engine, client, run):
                    tool = Mock(return_value="result")
                    engine.register_tool("probe", {"type": "function", "function": {
                        "name": "probe", "parameters": {"type": "object"},
                    }}, tool)
                    self.assertEqual(run(), "after")
                    tool.assert_called_once_with({})
                    self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_retry_limit_still_applies(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode), patch("enkiball.llm.MAX_REQUEST_RETRIES", 2):
                error = httpx.ReadError("unavailable")
                with self.request(async_mode, [error, error, error]) as (_, client, run):
                    with self.assertRaises(httpx.ReadError):
                        run()
                    self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_retry_callback_failure_still_closes_client(self):
        for async_mode in (False, True):
            for compact in (False, True):
                with self.subTest(async_mode=async_mode, compact=compact):
                    on_retry = Mock(side_effect=RuntimeError("retry callback failed"))
                    with self.request(async_mode, [httpx.ReadError("disconnected")],
                                      compact=compact, on_retry=on_retry) as (_, client, run):
                        with self.assertRaisesRegex(RuntimeError, "retry callback failed"):
                            run()
                        self.assertEqual(client.chat.completions.create.call_count, 1)
