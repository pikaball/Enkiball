"""Local, literal search over saved conversation fields."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console

from ..session_store import SessionRecord, SessionStore


@dataclass(frozen=True)
class SessionSearchResult:
    record: SessionRecord
    source: str
    snippet: str


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text")
            and isinstance(part.get("text"), str)
        )
    return ""


def _tool_text(value: Any) -> str:
    # Tool arguments/results are often JSON strings with escaped Unicode.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _fields(
    record: SessionRecord, *, include_tool: bool, include_thinking: bool,
) -> Iterator[tuple[str, str]]:
    yield "title", record.name
    for message in record.messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in ("user", "assistant"):
            yield role, _content_text(message.get("content"))
        if role == "assistant":
            if include_thinking:
                yield "thinking", _content_text(message.get("reasoning_content"))
            calls = message.get("tool_calls")
            if include_tool and isinstance(calls, list):
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict):
                        yield "tool call", _content_text(function.get("name"))
                        yield "tool arguments", _tool_text(function.get("arguments"))
        elif role == "tool" and include_tool:
            content = message.get("content")
            yield "tool result", _tool_text(_content_text(content) if isinstance(content, list) else content)


def search_sessions(
    store: SessionStore,
    keyword: str,
    *,
    global_search: bool = False,
    include_tool: bool = False,
    include_thinking: bool = False,
) -> list[SessionSearchResult]:
    """Match titles/text in the exact cwd, newest first, once per session.

    Global search includes legacy sessions without a cwd. System prompts,
    provider payloads, image data and storage metadata are never searched.
    """
    if not keyword.strip():
        raise ValueError("search requires a non-empty keyword")
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    results = []
    for record in store.list_sessions(ignore_cwd=True):
        if not global_search:
            try:
                if not record.cwd or Path(record.cwd).expanduser().resolve() != store.cwd:
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
        for source, text in _fields(record, include_tool=include_tool, include_thinking=include_thinking):
            match = pattern.search(text)
            if match is None:
                continue
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 100)
            snippet = ("…" if start else "") + " ".join(text[start:end].split())
            if end < len(text):
                snippet += "…"
            results.append(SessionSearchResult(record, source, snippet))
            break
    return results


def print_search_results(console: Console, results: list[SessionSearchResult], keyword: str) -> None:
    if not results:
        console.print(f"No matching sessions for {keyword!r}.", markup=False, highlight=False)
        return
    console.print(f"Found {len(results)} matching session(s):", markup=False, highlight=False)
    for result in results:
        record = result.record
        console.print(f"\n{record.session_id}  {record.name}", markup=False, highlight=False, soft_wrap=True)
        console.print(f"  cwd: {record.cwd or '(unknown)'}  updated: {record.updated_at}",
                      markup=False, highlight=False, soft_wrap=True)
        console.print(f"  {result.source}: {result.snippet}", markup=False, highlight=False, soft_wrap=True)
