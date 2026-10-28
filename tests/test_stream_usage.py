from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletionChunk

from enkiball.llm import ChatEngine


def chunk(delta=None, usage=None):
    return ChatCompletionChunk.model_validate({
        "id": "test", "object": "chat.completion.chunk", "created": 0,
        "model": "test",
        "choices": [] if delta is None else [{"index": 0, "delta": delta}],
        "usage": usage,
    })


class Stream:
    def __init__(self, events):
        self.events = events

    def __iter__(self):
        return iter(self.events)

    async def __aiter__(self):
        for event in self.events:
            yield event

    def close(self):
        pass


class StreamUsageTests(unittest.TestCase):
    def events(self, reasoning_key="reasoning_content"):
        usage = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
        return [chunk({reasoning_key: "consider"}), chunk({"content": "answer"}, usage),
                chunk(usage=usage)]

    def test_usage_with_choices_and_repeated_final_usage(self):
        for async_mode in (False, True):
            for final_chunk in (False, True):
                with self.subTest(async_mode=async_mode, final_chunk=final_chunk):
                    events = self.events()
                    if not final_chunk:
                        events.pop()
                    collect = ChatEngine._acollect_stream_output if async_mode else ChatEngine._collect_stream_output
                    output = collect(Stream(events), lambda _: None)
                    if async_mode:
                        output = asyncio.run(output)
                    self.assertEqual(output[:2], ("answer", "consider"))
                    self.assertEqual((output[-1].prompt_tokens, output[-1].completion_tokens), (12, 7))

    def test_usage_only_chunk_and_missing_usage(self):
        output = ChatEngine._collect_stream_output(Stream([chunk({"content": "ok"})]), lambda _: None)
        self.assertEqual(output[-1].prompt_tokens, 0)
        output = ChatEngine._collect_stream_output(Stream([self.events()[-1]]), lambda _: None)
        self.assertEqual(output[-1].completion_tokens, 7)

    def test_thinking_capture_and_callbacks_ignore_display_toggle(self):
        for async_mode in (False, True):
            for reasoning_key in ("reasoning", "reasoning_content"):
                with self.subTest(async_mode=async_mode, reasoning_key=reasoning_key):
                    engine = ChatEngine({}, state={}, model="test")
                    seen = []
                    events = self.events(reasoning_key)
                    client = Mock()
                    client.chat.completions.create.return_value = Stream(events)
                    if async_mode:
                        async def create(**kwargs):
                            return Stream(events)
                        async def close():
                            pass
                        client.chat.completions.create = create
                        client.close = close
                    with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                        reply = engine.astream_reply if async_mode else engine.stream_reply
                        result = reply("hi", [], lambda _: None, on_thinking=seen.append)
                        if async_mode:
                            result = asyncio.run(result)
                    self.assertEqual(result, "answer")
                    self.assertFalse(engine.show_thinking)
                    self.assertEqual(seen, ["consider"])
                    self.assertEqual(engine.capture().content, "answer")
                    self.assertEqual(engine.capture(collect_thinking=True).content, "consideranswer")
                    self.assertFalse(engine.capture().in_progress)
                    self.assertEqual(engine.messages[-1]["reasoning_content"], "consider")
                    self.assertEqual(engine.token_usage.total_tokens, 19)

    def test_compact_counts_usage_attached_to_content(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                engine = ChatEngine({}, state={}, model="test")
                client = Mock()
                client.chat.completions.create.return_value = Stream(self.events()[:-1])
                if async_mode:
                    async def create(**kwargs):
                        return Stream(self.events()[:-1])
                    async def close():
                        pass
                    client.chat.completions.create = create
                    client.close = close
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    result = engine.acompact() if async_mode else engine.compact()
                    if async_mode:
                        result = asyncio.run(result)
                self.assertEqual(result, "answer")
                self.assertEqual(engine.token_usage.total_tokens, 19)

    def test_model_override_uses_its_own_parameters(self):
        engine = ChatEngine({"providers": {"p": {"models": {
            "one": {"max_tokens": 100}, "two": {"max_tokens": 200}
        }}}}, state={"active_provider": "p", "active_model": "one"}, model="two")
        self.assertEqual(engine._build_compact_request(None)["max_tokens"], 200)
        self.assertEqual(engine._auto_compact_effective_threshold_tokens(), 140)
