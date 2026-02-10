from __future__ import annotations

import base64
import json
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from openai import OpenAI

from .llm_tools import TOOL_SCHEMAS, dispatch_tool
from .mcp import MCPManager


SYSTEM_PROMPT = (
    "You are Enkiball, a practical assistant for security research tasks. "
    "Be direct, careful, and evidence-driven. "
    "When file access or shell execution is needed, use the provided tools."
)


def _default_messages() -> list[dict[str, Any]]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


@dataclass
class ParsedToolCall:
    tool_call_id: str
    name: str
    arguments_raw: str


class ChatEngine:
    def __init__(self, config: dict[str, Any], mcp_manager: MCPManager | None = None) -> None:
        self.config = config
        self.mcp_manager = mcp_manager
        self.messages: list[dict[str, Any]] = _default_messages()

    @property
    def model(self) -> str:
        return str(self.config["llm"]["model"])

    @model.setter
    def model(self, new_model: str) -> None:
        self.config["llm"]["model"] = new_model

    def _client(self) -> OpenAI:
        api_key = os.getenv("API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing API key. Set `API_KEY` in `.env`.")
        kwargs: dict[str, Any] = {"api_key": api_key}
        base_url = os.getenv("BASE_URL", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs)

    def _get_tools(self) -> list[dict[str, Any]]:
        tools = list(TOOL_SCHEMAS)
        if self.mcp_manager:
            tools.extend(self.mcp_manager.get_tools())
        return tools

    def reset_messages(self) -> None:
        self.messages = _default_messages()

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            self.messages = messages
            return
        self.messages = _default_messages() + messages

    def export_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    @staticmethod
    def _persist_mcp_image(base64_data: str, mime_type: str | None = None) -> str:
        image_dir = Path("data/mcp_images")
        image_dir.mkdir(parents=True, exist_ok=True)
        mime = mime_type or "application/octet-stream"
        ext = mimetypes.guess_extension(mime) or ".bin"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = image_dir / f"{ts}-{uuid4().hex[:8]}{ext}"
        raw = base64.b64decode(base64_data)
        target.write_bytes(raw)
        return str(target)

    @staticmethod
    def _extract_user_content(content: Any) -> tuple[str, list[str]]:
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content), []

        texts: list[str] = []
        images: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            part_type = item.get("type")
            if part_type == "text":
                texts.append(str(item.get("text", "")))
                continue
            if part_type == "input_image":
                path = item.get("path")
                if isinstance(path, str) and path:
                    images.append(path)
        return "".join(texts), images

    @staticmethod
    def _build_user_content(user_text: str, image_paths: list[str]) -> str | list[dict[str, Any]]:
        normalized = [str(Path(p).expanduser().resolve()) for p in image_paths]
        if not normalized:
            return user_text
        parts: list[dict[str, Any]] = []
        if user_text:
            parts.append({"type": "text", "text": user_text})
        for path in normalized:
            parts.append({"type": "input_image", "path": path})
        return parts

    @staticmethod
    def _image_path_to_data_url(path: str) -> str:
        fp = Path(path).expanduser().resolve()
        if not fp.exists():
            raise FileNotFoundError(f"Image not found: {fp}")
        if not fp.is_file():
            raise IsADirectoryError(f"Not a file: {fp}")

        mime, _ = mimetypes.guess_type(str(fp))
        if not mime:
            mime = "application/octet-stream"
        payload = base64.b64encode(fp.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{payload}"

    @classmethod
    def _prepare_content_for_api(cls, content: Any) -> Any:
        if not isinstance(content, list):
            return content

        out: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "input_image":
                path = part.get("path")
                if not isinstance(path, str) or not path:
                    continue
                out.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": cls._image_path_to_data_url(path)},
                    }
                )
                continue
            if part_type == "text":
                out.append({"type": "text", "text": str(part.get("text", ""))})
                continue
            out.append(part)
        return out

    def _prepare_messages_for_api(self) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for msg in self.messages:
            item = dict(msg)
            item["content"] = self._prepare_content_for_api(item.get("content", ""))
            prepared.append(item)
        return prepared

    def revert_user_turns(self, steps: int = 1) -> tuple[int, str | None, list[str]]:
        if steps < 1:
            return 0, None, []

        reverted = 0
        prefill: str | None = None
        restored_images: list[str] = []
        while reverted < steps:
            user_idx: int | None = None
            for idx in range(len(self.messages) - 1, -1, -1):
                if self.messages[idx].get("role") == "user":
                    user_idx = idx
                    break
            if user_idx is None:
                break

            removed_user = self.messages[user_idx].get("content", "")
            self.messages = self.messages[:user_idx]
            prefill, restored_images = self._extract_user_content(removed_user)
            reverted += 1

        if not self.messages:
            self.messages = _default_messages()
        return reverted, prefill, restored_images

    @staticmethod
    def _parse_tool_args(raw_args: str) -> dict[str, Any]:
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _flatten_mcp_content(mcp_res: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "raw": mcp_res}
        if not isinstance(mcp_res, dict) or "content" not in mcp_res:
            return payload

        content_list = mcp_res.get("content", [])
        texts: list[str] = []
        saved_images: list[str] = []
        for item in content_list:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif item.get("type") == "image":
                data = item.get("data")
                if isinstance(data, str) and data:
                    try:
                        path = ChatEngine._persist_mcp_image(data, item.get("mimeType"))
                        saved_images.append(path)
                        texts.append(f"[Image saved: {path}]")
                        continue
                    except Exception:
                        pass
                texts.append("[Image data]")
        payload["text"] = "\n".join(texts)
        if saved_images:
            payload["images"] = saved_images
        payload["is_error"] = bool(mcp_res.get("isError"))
        return payload

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        if not name.startswith("mcp__"):
            return dispatch_tool(name, args)

        if not self.mcp_manager:
            return json.dumps({"ok": False, "error": "MCP manager not initialized"}, ensure_ascii=False)

        try:
            mcp_res = self.mcp_manager.call_tool(name, args)
            return json.dumps(self._flatten_mcp_content(mcp_res), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @staticmethod
    def _collect_stream_output(
        stream: Any,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, list[ParsedToolCall], bool]:
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        interrupted = False

        try:
            for event in stream:
                if should_stop and should_stop():
                    interrupted = True
                    break
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    on_token(delta.content)

                if not delta.tool_calls:
                    continue
                for tc in delta.tool_calls:
                    idx = int(tc.index or 0)
                    entry = tool_calls_acc.setdefault(
                        idx,
                        {"id": "", "name": "", "arguments_parts": []},
                    )
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function and tc.function.name:
                        entry["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        entry["arguments_parts"].append(tc.function.arguments)
        finally:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                close_fn()

        tool_calls: list[ParsedToolCall] = []
        for idx in sorted(tool_calls_acc):
            item = tool_calls_acc[idx]
            tool_calls.append(
                ParsedToolCall(
                    tool_call_id=item["id"],
                    name=item["name"],
                    arguments_raw="".join(item["arguments_parts"]) or "{}",
                )
            )
        return "".join(content_parts), tool_calls, interrupted

    @staticmethod
    def _append_assistant_message(
        messages: list[dict[str, Any]],
        assistant_content: str,
        tool_calls: list[ParsedToolCall],
    ) -> None:
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.tool_call_id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments_raw},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

    def stream_reply(
        self,
        user_input: str,
        image_paths: list[str] | None,
        on_token: Callable[[str], None],
        on_tool_event: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        content = self._build_user_content(user_input, image_paths or [])
        self.messages.append({"role": "user", "content": content})

        llm_cfg = self.config["llm"]
        client = self._client()

        for _ in range(64):
            if should_stop and should_stop():
                return ""
            current_tools = self._get_tools()
            kwargs: dict[str, Any] = {}
            if current_tools:
                kwargs["tools"] = current_tools
                kwargs["tool_choice"] = "auto"

            stream: Any = client.chat.completions.create(
                model=llm_cfg["model"],
                messages=self._prepare_messages_for_api(),
                temperature=llm_cfg.get("temperature", 0.2),
                max_tokens=llm_cfg.get("max_tokens", 2048),
                stream=True,
                **kwargs,
            )

            assistant_content, tool_calls, interrupted = self._collect_stream_output(
                stream,
                on_token,
                should_stop=should_stop,
            )
            if interrupted or (should_stop and should_stop()):
                if assistant_content:
                    self.messages.append({"role": "assistant", "content": assistant_content})
                return assistant_content
            self._append_assistant_message(self.messages, assistant_content, tool_calls)

            if not tool_calls:
                return assistant_content

            for tc in tool_calls:
                args = self._parse_tool_args(tc.arguments_raw)
                if on_tool_event:
                    on_tool_event(f"{tc.name}({tc.arguments_raw})")
                result = self._dispatch_tool(tc.name, args)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.tool_call_id,
                        "content": result,
                    }
                )

        fallback = "Tool-calling loop reached limit. Please refine the request."
        self.messages.append({"role": "assistant", "content": fallback})
        on_token(fallback)
        return fallback
