from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import APIError

from enkiball.config import (
    DEFAULT_CONFIG,
    fetch_remote_models,
    get_provider_models,
    get_provider_protocol,
    load_config,
)
from enkiball.llm import ChatEngine
from enkiball.protocols import (AsyncProtocolStream, EventDecoder, ProtocolStream,
                               api_base_url, message_fingerprint, prepare_request)
from enkiball.message_codec import prepare_messages_for_api
from enkiball.session_store import SessionStore


TOOL = {"type": "function", "function": {"name": "probe", "description": "test",
    "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}


def events(protocol, tool=False, text="done"):
    prompt, completion = (100, 10) if tool else (120, 20)
    if protocol == "chat-completions":
        delta = {"content": text}
        if tool:
            delta = {"content": "checking", "reasoning_content": "think", "tool_calls": [{
                "index": 0, "id": "call-1", "type": "function", "function": {"name": "probe", "arguments": '{"n":7}'},
            }]}
        base = {"id": "reply", "created": 0, "object": "chat.completion.chunk", "model": "test"}
        usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        return [{**base, "choices": [{"index": 0, "delta": delta}], "usage": usage},
                {**base, "choices": [], "usage": usage}]
    if protocol == "responses":
        output, result = [], []
        if tool:
            item = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "think"}], "encrypted_content": "encrypted"}
            output.append(item)
            result.extend([{"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "think"},
                           {"type": "response.output_item.done", "output_index": 0, "item": item}])
        index = len(output)
        item = {"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                "content": [{"type": "output_text", "text": "checking" if tool else text, "annotations": []}]}
        output.append(item)
        result.extend([{"type": "response.output_text.delta", "output_index": index, "delta": item["content"][0]["text"]},
                       {"type": "response.output_item.done", "output_index": index, "item": item}])
        if tool:
            item = {"type": "function_call", "id": "fc_1", "call_id": "call-1", "name": "probe", "arguments": '{"n":7}', "status": "completed"}
            output.append(item)
            result.extend([{"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"n":'},
                           {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '7}'},
                           {"type": "response.output_item.done", "output_index": 2, "item": item}])
        result.append({"type": "response.completed", "response": {"output": output, "usage": {"input_tokens": prompt, "output_tokens": completion}}})
        return result
    result = [{"type": "message_start", "message": {"usage": {
        "input_tokens": prompt - 30, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 1,
    }}}]
    if tool:
        result.extend([
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "think"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
            {"type": "content_block_stop", "index": 0},
        ])
    index = 1 if tool else 0
    result.extend([
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": "checking" if tool else text}},
        {"type": "content_block_stop", "index": index},
    ])
    if tool:
        result.extend([
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call-1", "name": "probe", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"n":'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '7}'}},
            {"type": "content_block_stop", "index": 2},
        ])
    result.extend([{"type": "message_delta", "usage": {"output_tokens": completion}}, {"type": "message_stop"}])
    return result


def sse(items):
    return "".join("data: " + json.dumps(item) + "\n\n" for item in items).encode()


def engine_for(protocol):
    return ChatEngine({"providers": {"local": {"protocol": protocol, "api_key": "test-secret",
        "base_url": "http://test/v1", "models": {"test": {"max_tokens": 256, "temperature": 0.2}}}}}, state={}, model="test")


class ProtocolConfigTests(unittest.TestCase):
    def test_defaults_and_invalid_protocols(self):
        self.assertEqual(DEFAULT_CONFIG["providers"]["openai"]["protocol"], "responses")
        self.assertEqual(get_provider_protocol({}, state={}), "responses")
        for value in (None, "response", "chat-completion", "chat", "", []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid protocol"):
                engine_for(value)

    def test_load_rejects_invalid_protocol_before_persisting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"providers": {"p": {"protocol": "typo"}}, "active_provider": "p"}))
            with self.assertRaisesRegex(ValueError, "invalid protocol"):
                load_config(config, root / "state.json")
            self.assertFalse((root / "state.json").exists())

    def test_provider_switch_selects_protocol_and_urls(self):
        engine = engine_for("chat-completions")
        engine.config["providers"]["other"] = {"protocol": "anthropic"}
        engine.state["active_provider"] = "other"
        self.assertEqual(engine.protocol, "anthropic")
        self.assertEqual(api_base_url("anthropic", ""), "https://api.anthropic.com/v1")
        self.assertEqual(api_base_url("anthropic", "http://localhost:4011/anthropic"), "http://localhost:4011/anthropic/v1")
        self.assertEqual(api_base_url("responses", "http://localhost:4011"), "http://localhost:4011/v1")

    def test_anthropic_model_discovery_uses_native_headers(self):
        with patch("enkiball.config.httpx.get", return_value=httpx.Response(200, json={"data": [{"id": "model"}]}, request=httpx.Request("GET", "http://test"))) as get:
            self.assertEqual(fetch_remote_models("key", "https://api.anthropic.com", protocol="anthropic"), ["model"])
        self.assertEqual(get.call_args.args[0], "https://api.anthropic.com/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["x-api-key"], "key")

    def test_model_discovery_without_api_key_sends_no_auth_header(self):
        with patch("enkiball.config.httpx.get", return_value=httpx.Response(200, json={"data": [{"id": "qwen3:9b"}]}, request=httpx.Request("GET", "http://test"))) as get:
            self.assertEqual(
                fetch_remote_models("", "http://127.0.0.1:11434/v1",
                                    protocol="chat-completions"),
                ["qwen3:9b"],
            )
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_endpoint_models_are_merged_with_configured_overrides(self):
        config = {
            "providers": {
                "local": {
                    "protocol": "chat-completions",
                    "api_key": "",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "models": {"qwen3:9b": {"max_tokens": 2048}},
                }
            }
        }
        with patch(
            "enkiball.config.fetch_remote_models",
            return_value=["llama3:8b", "qwen3:9b"],
        ) as fetch:
            models = get_provider_models(config)
        self.assertEqual(models, ["llama3:8b", "qwen3:9b"])
        self.assertEqual(fetch.call_args.args[:2], ("", "http://127.0.0.1:11434/v1"))

        with patch("enkiball.config.fetch_remote_models", return_value=["llama3:8b"]):
            self.assertEqual(
                get_provider_models(config),
                ["llama3:8b", "qwen3:9b"],
            )


class ProtocolRoundTripTests(unittest.TestCase):
    def test_default_provider_uses_responses_on_the_wire(self):
        engine = engine_for("responses")
        del engine.config["providers"]["local"]["protocol"]
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, content=sse(events("responses")), headers={"content-type": "text/event-stream"})
        with patch("enkiball.llm.InterruptibleTransport", side_effect=lambda _: httpx.MockTransport(handler)):
            self.assertEqual(engine.stream_reply("hi", [], lambda _: None), "done")
        self.assertEqual(seen, ["/v1/responses"])

    def test_sync_async_tool_loop_usage_replay_and_compact(self):
        for protocol in ("chat-completions", "responses", "anthropic"):
            for async_mode in (False, True):
                with self.subTest(protocol=protocol, async_mode=async_mode):
                    engine = engine_for(protocol)
                    calls, requests, headers, tokens, thinking = [], [], [], [], []
                    engine.register_tool("probe", TOOL, lambda args: calls.append(args) or "result-seven")

                    def handler(request):
                        requests.append(json.loads(request.content))
                        headers.append(dict(request.headers))
                        self.assertEqual(request.url.path, {"chat-completions": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/v1/messages"}[protocol])
                        data = sse(events(protocol, tool=len(requests) == 1))
                        return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})

                    transport = "InterruptibleAsyncTransport" if async_mode else "InterruptibleTransport"
                    with patch("enkiball.llm." + transport, side_effect=lambda _: httpx.MockTransport(handler)):
                        if async_mode:
                            answer = asyncio.run(engine.astream_reply("question", [], tokens.append, on_thinking=thinking.append))
                        else:
                            answer = engine.stream_reply("question", [], tokens.append, on_thinking=thinking.append)
                        self.assertEqual(answer, "done")
                        self.assertEqual(calls, [{"n": 7}])
                        self.assertEqual("".join(tokens), "checkingdone")
                        self.assertEqual(thinking, ["think"])
                        self.assertEqual(engine.token_usage.total_tokens, 250)
                        self.assertEqual(engine.context_tokens, 140)
                        second = requests[1]
                        self.assertNotIn("_provider_data", json.dumps(second))
                        if protocol == "responses":
                            self.assertEqual(second["max_output_tokens"], 256)
                            self.assertNotIn("max_tokens", second)
                            self.assertFalse(second["store"])
                            self.assertFalse(second["tools"][-1]["strict"])
                            self.assertTrue(any(i.get("encrypted_content") == "encrypted" for i in second["input"]))
                            self.assertTrue(any(i.get("type") == "function_call_output" and i["call_id"] == "call-1" for i in second["input"]))
                        elif protocol == "anthropic":
                            self.assertEqual(headers[0]["x-api-key"], "test-secret")
                            self.assertEqual(headers[0]["anthropic-version"], "2023-06-01")
                            self.assertTrue(second["system"])
                            self.assertFalse(any(m["role"] == "system" for m in second["messages"]))
                            blocks = [b for m in second["messages"] for b in m["content"]]
                            self.assertTrue(any(b.get("signature") == "signed" for b in blocks))
                            self.assertTrue(any(b.get("tool_use_id") == "call-1" for b in blocks))
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            store = SessionStore._from_paths(root / "sessions", root / "exports")
                            saved = store.create(initial_messages=engine.export_messages())
                            restored = engine_for(protocol)
                            restored.load_messages(store.load(saved.session_id).messages)
                            self.assertEqual(restored.messages, engine.messages)
                            self.assertEqual(restored.token_usage.total_tokens, 250)
                        if async_mode:
                            self.assertEqual(asyncio.run(engine.acompact()), "done")
                        else:
                            self.assertEqual(engine.compact(), "done")
                        self.assertEqual(engine.token_usage.total_tokens, 390)
                        self.assertNotIn("tools", requests[-1])
                        self.assertGreater(engine.compact_index, 0)

    def test_native_history_is_not_replayed_to_other_protocols_or_models(self):
        for source in ("responses", "anthropic"):
            decoder = EventDecoder(source, "test", "local")
            native = None
            for event in events(source, tool=True):
                for part in decoder.decode(event):
                    native = getattr(part, "provider_data", None) or native
            message = {"role": "assistant", "content": "checking", "tool_calls": [{
                "id": "call-1", "type": "function", "function": {"name": "probe", "arguments": '{"n":7}'},
            }], "_provider_data": native}
            for target in ("chat-completions", "responses", "anthropic"):
                for model in ("test", "other"):
                    request = {"model": model, "messages": [message], "max_tokens": 256}
                    _, body = prepare_request(target, request, "local")
                    if target != source or model != "test":
                        self.assertNotIn('"encrypted_content": "encrypted"', json.dumps(body))
                        self.assertNotIn("signed", json.dumps(body))
                    self.assertNotIn("_provider_data", json.dumps(body))

    def test_multimodal_tool_results_and_extra_body(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
        messages = prepare_messages_for_api([
            {"role": "user", "content": [image]},
            {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "probe", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c", "content": [{"type": "text", "text": "image result"}, image]},
        ])
        for protocol in ("chat-completions", "responses", "anthropic"):
            request = {"model": "test", "messages": messages, "max_tokens": 256, "extra_body": {"temperature": 0.1}}
            original = copy.deepcopy(request)
            _, body = prepare_request(protocol, request)
            self.assertEqual(request, original)
            self.assertEqual(body["temperature"], 0.1)
            self.assertEqual(json.dumps(body).count("aGVsbG8="), 2)
            if protocol == "anthropic":
                self.assertEqual(body["messages"][0]["content"][0]["source"]["media_type"], "image/png")

    def test_editing_imported_history_invalidates_native_output(self):
        for protocol in ("responses", "anthropic"):
            decoder = EventDecoder(protocol, "test", "local")
            native = None
            for item in events(protocol):
                for part in decoder.decode(item):
                    native = getattr(part, "provider_data", None) or native
            message = {"role": "assistant", "content": "done", "_provider_data": native}
            native["message_hash"] = message_fingerprint(message)
            message["content"] = "edited history"
            _, body = prepare_request(protocol, {"model": "test", "messages": [message], "max_tokens": 256}, "local")
            self.assertIn("edited history", json.dumps(body))
            self.assertNotIn('"done"', json.dumps(body))


class ProtocolErrorTests(unittest.TestCase):
    def test_terminal_event_does_not_wait_for_connection_close(self):
        for protocol in ("responses", "anthropic"):
            class Stream:
                def __iter__(self):
                    yield from events(protocol)
                    raise AssertionError("read past terminal event")
                async def __aiter__(self):
                    for item in self:
                        yield item
            list(ProtocolStream(Stream(), EventDecoder(protocol, "test")))
            async def collect():
                return [item async for item in AsyncProtocolStream(Stream(), EventDecoder(protocol, "test"))]
            asyncio.run(collect())

    def test_incomplete_function_call_is_not_executed(self):
        decoder = EventDecoder("responses", "test")
        with self.assertRaisesRegex(RuntimeError, "incomplete function call"):
            decoder.decode({"type": "response.incomplete", "response": {"output": [{
                "type": "function_call", "status": "incomplete", "call_id": "call-1", "name": "probe", "arguments": '{"n":',
            }]}})

    def test_nonterminal_streams_and_failed_responses_are_errors(self):
        for protocol in ("responses", "anthropic"):
            decoder = EventDecoder(protocol, "test")
            for item in events(protocol)[:-1]:
                decoder.decode(item)
            with self.assertRaises(httpx.RemoteProtocolError):
                decoder.finish()
        with self.assertRaisesRegex(RuntimeError, "response failed"):
            EventDecoder("responses", "test").decode({"type": "response.failed", "response": {"error": "bad"}})

    def test_http_errors_keep_retry_classification(self):
        for protocol in ("chat-completions", "responses", "anthropic"):
            for async_mode in (False, True):
                engine = engine_for(protocol)
                count = []
                def handler(request):
                    count.append(1)
                    if len(count) == 1:
                        return httpx.Response(429, json={"error": {"message": "busy"}})
                    return httpx.Response(200, content=sse(events(protocol)), headers={"content-type": "text/event-stream"})
                transport = "InterruptibleAsyncTransport" if async_mode else "InterruptibleTransport"
                with patch("enkiball.llm." + transport, side_effect=lambda _: httpx.MockTransport(handler)), patch.object(
                    engine, "_await_retry" if async_mode else "_wait_for_retry", return_value=True
                ):
                    if async_mode:
                        answer = asyncio.run(engine.astream_reply("hi", [], lambda _: None))
                    else:
                        answer = engine.stream_reply("hi", [], lambda _: None)
                self.assertEqual(answer, "done")
                self.assertEqual(len(count), 2)

    def test_sse_error_never_becomes_a_successful_empty_answer(self):
        for protocol in ("responses", "anthropic"):
            engine = engine_for(protocol)
            def handler(request):
                return httpx.Response(200, content=sse([{"type": "error", "error": {"message": "bad stream"}}]), headers={"content-type": "text/event-stream"})
            with patch("enkiball.llm.InterruptibleTransport", side_effect=lambda _: httpx.MockTransport(handler)):
                with self.assertRaises(APIError):
                    engine.stream_reply("hi", [], lambda _: None)
            self.assertEqual(engine.messages[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
