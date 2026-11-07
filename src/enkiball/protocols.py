"""Translate the engine's common message format to provider wire protocols.

OpenAI's low-level streaming client supplies HTTP errors, SSE decoding and
resource cleanup for all three protocols. No server-side conversation state
is required: native output needed for replay is stored in session messages.
"""

from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI, AsyncStream, OpenAI, Stream
from openai.types.chat import ChatCompletionChunk


def api_base_url(protocol: str, base_url: str) -> str:
    base = base_url.rstrip("/") if base_url else (
        "https://api.anthropic.com/v1" if protocol == "anthropic" else "https://api.openai.com/v1"
    )
    path = urlsplit(base).path.rstrip("/")
    if not path or (protocol == "anthropic" and not path.endswith("/v1")):
        base += "/v1"
    return base


def _native(msg: dict[str, Any], protocol: str, model: str, provider: str) -> list[dict[str, Any]] | None:
    data = msg.get("_provider_data")
    if isinstance(data, dict) and (
        data.get("protocol"), data.get("model"), data.get("provider")
    ) == (protocol, model, provider):
        if data.get("message_hash") and data["message_hash"] != message_fingerprint(msg):
            return None
        return copy.deepcopy(data.get("output"))
    return None


def message_fingerprint(msg: dict[str, Any]) -> str:
    portable = {key: msg.get(key) for key in ("content", "tool_calls", "reasoning_content")}
    return hashlib.sha256(json.dumps(portable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _responses_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part["text"]})
        elif part.get("type") == "image_url":
            image = part["image_url"]
            parts.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
        else:
            raise ValueError(f"unsupported Responses content type: {part.get('type')}")
    return parts


def _responses_input(messages: list[dict[str, Any]], model: str, provider: str) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        role = msg["role"]
        native = _native(msg, "responses", model, provider) if role == "assistant" else None
        if native is not None:
            result.extend(native)
            continue
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": msg["tool_call_id"], "output": msg.get("content", "")})
            continue
        content = _responses_content(msg.get("content", ""))
        if content:
            result.append({"role": role, "content": content})
        for call in msg.get("tool_calls", []):
            result.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    return result


def _anthropic_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    result = []
    for part in content or []:
        if part.get("type") == "text":
            if part.get("text"):
                result.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            if url.startswith("data:"):
                header, data = url.split(",", 1)
                if not header.endswith(";base64"):
                    raise ValueError("Anthropic images require base64 data URLs")
                source = {"type": "base64", "media_type": header[5:-7], "data": data}
            else:
                source = {"type": "url", "url": url}
            result.append({"type": "image", "source": source})
        else:
            raise ValueError(f"unsupported Anthropic content type: {part.get('type')}")
    return result


def _anthropic_messages(messages: list[dict[str, Any]], model: str, provider: str) -> tuple[list, list]:
    system, result = [], []
    for msg in messages:
        role = msg["role"]
        if role in {"system", "developer"}:
            system.extend(_anthropic_content(msg.get("content", "")))
            continue
        native = _native(msg, "anthropic", model, provider) if role == "assistant" else None
        if native is not None:
            blocks = native
        elif role == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg.get("content", "")}]
            role = "user"
        else:
            blocks = _anthropic_content(msg.get("content", ""))
            for call in msg.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
        if blocks:
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(blocks)
            else:
                result.append({"role": role, "content": blocks})
    return system, result


def prepare_request(protocol: str, request: dict[str, Any], provider: str = "") -> tuple[str, dict[str, Any]]:
    """Convert a common request; extra_body contains protocol-native options."""
    body = copy.deepcopy(request)
    extra = body.pop("extra_body", {}) or {}
    if not isinstance(extra, dict):
        raise TypeError("extra_body must be an object")
    if protocol == "chat-completions":
        for msg in body["messages"]:
            msg.pop("_provider_data", None)
        endpoint = "/chat/completions"
    elif protocol == "responses":
        body["input"] = _responses_input(body.pop("messages"), body["model"], provider)
        body["max_output_tokens"] = body.pop("max_tokens")
        body.pop("stream_options", None)
        body["store"] = False
        body["include"] = ["reasoning.encrypted_content"]
        if "tools" in body:
            body["tools"] = [dict(type="function", **tool["function"]) for tool in body["tools"]]
            # Responses defaults to strict schemas; existing tools may have optional fields.
            for tool in body["tools"]:
                tool.setdefault("strict", False)
        endpoint = "/responses"
    elif protocol == "anthropic":
        system, messages = _anthropic_messages(body.pop("messages"), body["model"], provider)
        body["messages"] = messages
        if system:
            body["system"] = system
        body.pop("stream_options", None)
        if "tools" in body:
            body["tools"] = [{"name": tool["function"]["name"], "description": tool["function"].get("description", ""),
                              "input_schema": tool["function"]["parameters"]} for tool in body["tools"]]
        if "tool_choice" in body:
            body["tool_choice"] = {"type": body["tool_choice"]}
        endpoint = "/messages"
    else:
        raise ValueError(f"unsupported protocol: {protocol}")
    body.update(extra)
    return endpoint, body


def _event(*, text: str = "", thinking: str = "", call: dict | None = None,
           usage: dict | None = None, native: dict | None = None) -> Any:
    calls = [] if call is None else [SimpleNamespace(
        index=call["index"], id=call["id"],
        function=SimpleNamespace(name=call["name"], arguments=call["arguments"]),
    )]
    delta = SimpleNamespace(content=text, reasoning_content=thinking, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)] if text or thinking or call else [],
                           usage=SimpleNamespace(**usage) if usage is not None else None,
                           provider_data=native)


class EventDecoder:
    def __init__(self, protocol: str, model: str, provider: str = "") -> None:
        self.protocol, self.model, self.provider = protocol, model, provider
        self.finished = False
        self.items: dict[int, dict[str, Any]] = {}
        self.emitted_calls: set[int] = set()
        self.text_seen: set[int] = set()
        self.thinking_seen: set[int] = set()
        self.arguments: dict[int, str] = {}
        self.usage: dict[str, int] = {}

    def _native(self, output: list[dict[str, Any]]) -> dict[str, Any]:
        return {"protocol": self.protocol, "model": self.model, "provider": self.provider, "output": copy.deepcopy(output)}

    def decode(self, data: dict[str, Any]) -> list[Any]:
        if self.protocol == "chat-completions":
            return [ChatCompletionChunk.model_validate(data)]
        if data.get("type") == "error":
            raise RuntimeError(f"provider stream error: {data.get('error', data.get('message', 'unknown error'))}")
        return self._responses(data) if self.protocol == "responses" else self._anthropic(data)

    def _response_item(self, index: int, item: dict[str, Any]) -> list[Any]:
        self.items[index] = item
        kind = item.get("type")
        if kind == "function_call" and index not in self.emitted_calls and item.get("status") != "incomplete":
            if not isinstance(json.loads(item.get("arguments", "{}")), dict):
                raise ValueError("function call arguments must be an object")
            self.emitted_calls.add(index)
            return [_event(call={"index": index, "id": item["call_id"], "name": item["name"], "arguments": item.get("arguments", "{}")})]
        if kind == "message" and index not in self.text_seen:
            self.text_seen.add(index)
            return [_event(text="".join(p.get("text", p.get("refusal", "")) for p in item.get("content", [])))]
        if kind == "reasoning" and index not in self.thinking_seen:
            self.thinking_seen.add(index)
            return [_event(thinking="".join(p.get("text", "") for p in item.get("summary", [])))]
        return []

    def _responses(self, data: dict[str, Any]) -> list[Any]:
        kind, index = data.get("type"), data.get("output_index", 0)
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            self.text_seen.add(index)
            return [_event(text=data.get("delta", ""))]
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            self.thinking_seen.add(index)
            return [_event(thinking=data.get("delta", ""))]
        if kind == "response.output_item.done":
            return self._response_item(index, data["item"])
        if kind == "response.failed":
            raise RuntimeError(f"response failed: {data.get('response', {}).get('error')}")
        if kind in {"response.completed", "response.incomplete"}:
            response = data["response"]
            if response.get("error"):
                raise RuntimeError(f"response failed: {response['error']}")
            output = response.get("output") or [self.items[k] for k in sorted(self.items)]
            if any(item.get("type") == "function_call" and item.get("status") in {"incomplete", "in_progress"} for item in output):
                raise RuntimeError("response ended with an incomplete function call")
            events = []
            for i, item in enumerate(output):
                events.extend(self._response_item(i, item))
            usage = response.get("usage") or {}
            normalized = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
            self.finished = True
            events.append(_event(usage=normalized, native=self._native(output)))
            return events
        return []

    def _anthropic_usage(self, usage: dict[str, Any]) -> dict[str, int]:
        self.usage.update({k: v for k, v in usage.items() if isinstance(v, int)})
        return {"prompt_tokens": sum(self.usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                "completion_tokens": self.usage.get("output_tokens", 0)}

    def _anthropic(self, data: dict[str, Any]) -> list[Any]:
        kind, index = data.get("type"), data.get("index", 0)
        if kind == "message_start":
            return [_event(usage=self._anthropic_usage(data.get("message", {}).get("usage", {})))]
        if kind == "content_block_start":
            block = self.items[index] = copy.deepcopy(data["content_block"])
            return [_event(text=block.get("text", ""), thinking=block.get("thinking", ""))]
        if kind == "content_block_delta":
            block, delta = self.items[index], data["delta"]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
                return [_event(text=delta["text"])]
            if dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
                return [_event(thinking=delta["thinking"])]
            if dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            if dtype == "input_json_delta":
                self.arguments[index] = self.arguments.get(index, "") + delta["partial_json"]
        if kind == "content_block_stop":
            block = self.items[index]
            if block.get("type") == "tool_use":
                args = self.arguments.get(index) or json.dumps(block.get("input", {}))
                block["input"] = json.loads(args)
                return [_event(call={"index": index, "id": block["id"], "name": block["name"], "arguments": args})]
        if kind == "message_delta":
            return [_event(usage=self._anthropic_usage(data.get("usage", {})))]
        if kind == "message_stop":
            self.finished = True
            return [_event(native=self._native([self.items[k] for k in sorted(self.items)]))]
        return []

    def finish(self) -> None:
        if self.protocol != "chat-completions" and not self.finished:
            raise httpx.RemoteProtocolError(f"{self.protocol} stream ended before its terminal event")


class ProtocolStream:
    def __init__(self, stream: Any, decoder: EventDecoder) -> None:
        self.stream, self.decoder = stream, decoder

    def __iter__(self):
        for data in self.stream:
            yield from self.decoder.decode(data)
            if self.decoder.finished:
                break
        self.decoder.finish()

    def close(self) -> None:
        self.stream.close()


class AsyncProtocolStream:
    def __init__(self, stream: Any, decoder: EventDecoder) -> None:
        self.stream, self.decoder = stream, decoder

    async def __aiter__(self):
        async for data in self.stream:
            for event in self.decoder.decode(data):
                yield event
            if self.decoder.finished:
                break
        self.decoder.finish()

    async def close(self) -> None:
        await self.stream.close()


class ProtocolClient:
    def __init__(self, client: OpenAI, protocol: str, provider: str) -> None:
        self.client, self.protocol, self.provider = client, protocol, provider

    def create(self, **request: Any) -> ProtocolStream:
        endpoint, body = prepare_request(self.protocol, request, self.provider)
        stream = self.client.post(endpoint, body=body, cast_to=dict, stream=True, stream_cls=Stream[dict[str, Any]])
        return ProtocolStream(stream, EventDecoder(self.protocol, body["model"], self.provider))

    def close(self) -> None:
        self.client.close()


class AsyncProtocolClient:
    def __init__(self, client: AsyncOpenAI, protocol: str, provider: str) -> None:
        self.client, self.protocol, self.provider = client, protocol, provider

    async def create(self, **request: Any) -> AsyncProtocolStream:
        endpoint, body = prepare_request(self.protocol, request, self.provider)
        stream = await self.client.post(endpoint, body=body, cast_to=dict, stream=True, stream_cls=AsyncStream[dict[str, Any]])
        return AsyncProtocolStream(stream, EventDecoder(self.protocol, body["model"], self.provider))

    async def close(self) -> None:
        await self.client.close()
