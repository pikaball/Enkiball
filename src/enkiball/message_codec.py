from __future__ import annotations

import base64
import mimetypes
from functools import lru_cache
from pathlib import Path
from typing import Any


def default_messages(system_prompt: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": system_prompt}]


def extract_user_content(content: Any) -> tuple[str, list[str]]:
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
        elif part_type == "input_image":
            path = item.get("path")
            if isinstance(path, str) and path:
                images.append(path)
    return "".join(texts), images


def build_user_content(user_text: str, image_paths: list[str]) -> str | list[dict[str, Any]]:
    normalized = [str(Path(p).expanduser().resolve()) for p in image_paths]
    if not normalized:
        return user_text

    parts: list[dict[str, Any]] = []
    if user_text:
        parts.append({"type": "text", "text": user_text})
    for path in normalized:
        parts.append({"type": "input_image", "path": path})
    return parts


def image_path_to_data_url(path: str) -> str:
    fp = Path(path).expanduser().resolve()
    if not fp.exists():
        raise FileNotFoundError(f"Image not found: {fp}")
    if not fp.is_file():
        raise IsADirectoryError(f"Not a file: {fp}")
    stat = fp.stat()
    return _cached_image_data_url(str(fp), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=32)
def _cached_image_data_url(path: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    fp = Path(path)
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "application/octet-stream"
    payload = base64.b64encode(fp.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def prepare_content_for_api(content: Any) -> Any:
    if not isinstance(content, list):
        return content

    out: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "input_image":
            path = part.get("path")
            if isinstance(path, str) and path:
                out.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_path_to_data_url(path)},
                    }
                )
        elif part_type == "text":
            out.append({"type": "text", "text": str(part.get("text", ""))})
        else:
            out.append(part)
    return out


def drop_orphan_tool_call_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove incomplete assistant/tool-call groups before API replay."""
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            if msg.get("role") != "tool":
                out.append(msg)
            i += 1
            continue

        tool_calls = msg.get("tool_calls") or []
        expected_ids = [
            str(tc.get("id", ""))
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("id")
        ]
        if not expected_ids:
            i += 1
            continue

        group = [msg]
        j = i + 1
        found: set[str] = set()
        valid = True
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_msg = messages[j]
            tool_call_id = str(tool_msg.get("tool_call_id", ""))
            if tool_call_id not in expected_ids or tool_call_id in found:
                valid = False
            else:
                found.add(tool_call_id)
                group.append(tool_msg)
            j += 1

        if valid and found == set(expected_ids):
            out.extend(group)
        i = j
    return out


def prepare_messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    pending_images: list[dict[str, Any]] = []
    for msg in drop_orphan_tool_call_messages(messages):
        # Chat Completions accepts images in user content, after all tool replies.
        if msg.get("role") != "tool" and pending_images:
            prepared.append({"role": "user", "content": pending_images})
            pending_images = []
        item = dict(msg)
        item.pop("_compact", None)
        item.pop("_usage", None)
        item["content"] = prepare_content_for_api(item.get("content", ""))
        if item.get("role") == "tool" and isinstance(item["content"], list):
            parts = item["content"]
            images = [part for part in parts if part.get("type") == "image_url"]
            if images:
                pending_images.append({
                    "type": "text",
                    "text": f"Image returned by tool call {item.get('tool_call_id', '')}",
                })
                pending_images.extend(images)
                item["content"] = "\n".join(
                    part.get("text", "") for part in parts if part.get("type") == "text"
                )
        prepared.append(item)
    if pending_images:
        prepared.append({"role": "user", "content": pending_images})
    return prepared
