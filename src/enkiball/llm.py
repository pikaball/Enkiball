from __future__ import annotations

import asyncio
import base64
import copy
import json
import mimetypes
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal
from uuid import uuid4

import httpx
from openai import AsyncOpenAI, OpenAI

from .config import (
    DEFAULT_STATE_PATH,
    get_active_model,
    get_model_params,
    get_system_prompt,
    load_state,
    resolve_provider_credentials,
    set_active_model,
)
from .llm_tools import TOOL_SCHEMAS, dispatch_tool
from .interruptible_http import InterruptibleAsyncTransport, InterruptibleTransport
from .message_codec import (
    build_user_content,
    default_messages,
    extract_user_content,
    prepare_messages_for_api,
)
from .mcp import MCPManager
from .sandbox import Sandbox, TurnInterrupted


SYSTEM_PROMPT = (
    "You are Enkiball, a practical assistant for security research tasks. "
    "Be direct, careful, and evidence-driven. "
    "When file access or shell execution is needed, use the provided tools."
)

DEFAULT_COMPACT_PROMPT = (
    "Summarize the conversation so far into a concise context block. "
    "Preserve all important facts, decisions, file paths, code snippets, "
    "tool results, and action items. The summary will replace prior messages "
    "as context for future turns, so do NOT omit details that may be needed later. "
    "Output only the summary, no preamble."
)

DEFAULT_AUTO_COMPACT_THRESHOLD_RATIO = 0.7
DEFAULT_ENDPOINT_MAX_TOKENS = 300000
TOOL_CALL_LIMIT = 512
MAX_REQUEST_RETRIES = 10
REQUEST_RETRY_DELAYS = (3, 5, 10, 30, 60, 120, 180)

@dataclass
class TokenUsage:
    """Accumulated token usage for the session."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def __repr__(self) -> str:
        return f"TokenUsage(prompt={self.prompt_tokens}, completion={self.completion_tokens}, total={self.total_tokens})"


@dataclass
class ParsedToolCall:
    tool_call_id: str
    name: str
    arguments_raw: str


@dataclass
class TurnUsage:
    """Token usage for a single API call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    def update(self, event: Any) -> None:
        usage = getattr(event, "usage", None)
        if usage is None:
            return
        # Stream usage is cumulative; a final chunk may repeat earlier counts.
        for name in ("prompt_tokens", "completion_tokens"):
            value = getattr(usage, name, None)
            if value is not None:
                setattr(self, name, int(value))


@dataclass
class RetryEvent:
    """Notification emitted before retrying a failed LLM request."""

    attempt: int
    max_retries: int
    delay_seconds: int
    next_retry_at: float
    error: str


@dataclass
class LiveSnapshot:
    """Snapshot of an in-flight (or just-finished) streaming turn.

    Returned by :meth:`Enkiball.capture`.  All fields are plain copies —
    safe to read from any thread without further locking.
    """

    content: str = ""
    thinking: str = ""
    tool_events: list[str] = field(default_factory=list)
    in_progress: bool = False
    started_at: float | None = None
    updated_at: float | None = None

    @property
    def elapsed(self) -> float:
        """Seconds since the turn started, or 0 if not started."""
        if self.started_at is None:
            return 0.0
        end = self.updated_at if not self.in_progress else time.monotonic()
        return max(0.0, (end or self.started_at) - self.started_at)


@dataclass
class TurnContext:
    turn_id: str
    started_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def interrupted(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()


class ChatEngine:
    def __init__(
        self,
        config: dict[str, Any],
        mcp_manager: MCPManager | None = None,
        sandbox: Sandbox | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        model: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.state_path = Path(state_path)
        self.state = copy.deepcopy(load_state(self.state_path) if state is None else state)
        self._model_override = model
        self.mcp_manager = mcp_manager
        self.sandbox = sandbox or Sandbox()
        # Use system prompt from config if set, otherwise use default
        prompt = get_system_prompt(config) or SYSTEM_PROMPT
        self._system_prompt = prompt
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        self.token_usage = TokenUsage()
        self.show_thinking: bool = False
        compact_cfg = config.get("auto_compact", {})
        if not isinstance(compact_cfg, dict):
            compact_cfg = {}
        self.auto_compact_enabled: bool = bool(compact_cfg.get("enabled", False))
        self.auto_compact_threshold_ratio: float = float(
            compact_cfg.get("threshold_ratio", DEFAULT_AUTO_COMPACT_THRESHOLD_RATIO)
            or DEFAULT_AUTO_COMPACT_THRESHOLD_RATIO
        )
        legacy_threshold = compact_cfg.get("threshold_tokens", compact_cfg.get("max_tokens"))
        self.auto_compact_threshold_tokens: int | None = (
            int(legacy_threshold) if legacy_threshold else None
        )
        self.auto_compact_prompt: str | None = compact_cfg.get("prompt") or None
        self._custom_tool_schemas: list[dict[str, Any]] = []
        # Custom tool handlers: name -> callable(args_dict) -> str
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._internal_tools: dict[str, dict[str, Any]] = {}
        self._internal_tool_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.notice_queue: list[str] = []
        self.compact_index: int = 0

        # Live streaming buffer — updated incrementally during stream_reply /
        # astream_reply so callers can `capture()` the in-progress output at
        # any moment.  Protected by a lock for thread-safe reads.
        self._live_lock = threading.Lock()
        self._live_content_parts: list[str] = []
        self._live_thinking_parts: list[str] = []
        self._live_tool_events: list[str] = []
        self._live_in_progress: bool = False
        self._live_started_at: float | None = None
        self._live_updated_at: float | None = None
        self._turn_lock = threading.Lock()
        self._turn_state_lock = threading.Lock()
        self._active_turn: TurnContext | None = None
        self._last_turn_interrupted = False

    # -- Auto compact --------------------------------------------------------

    def _api_visible_usage_total(self) -> int:
        for msg in reversed(self.messages[self.compact_index :]):
            usage = msg.get("_usage")
            if not isinstance(usage, dict):
                continue
            return int(usage.get("prompt_tokens", 0) or 0) + int(
                usage.get("completion_tokens", 0) or 0
            )
        return 0

    def _auto_compact_effective_threshold_tokens(self) -> int:
        if self.auto_compact_threshold_tokens is not None:
            return self.auto_compact_threshold_tokens
        model_params = get_model_params(self.config, self.model, state_path=self.state_path, state=self.state)
        endpoint_max_tokens = int(
            model_params.get("max_tokens", DEFAULT_ENDPOINT_MAX_TOKENS)
            or DEFAULT_ENDPOINT_MAX_TOKENS
        )
        return int(endpoint_max_tokens * self.auto_compact_threshold_ratio)

    def _should_auto_compact(self) -> bool:
        threshold_tokens = self._auto_compact_effective_threshold_tokens()
        return (
            self.auto_compact_enabled
            and threshold_tokens > 0
            and self._api_visible_usage_total() >= threshold_tokens
        )

    def _auto_compact_if_needed(
        self,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
    ) -> str | None:
        if not self._should_auto_compact():
            return None
        return self._compact_impl(
            prompt=self.auto_compact_prompt, on_token=on_token, on_retry=on_retry
        )

    async def _aauto_compact_if_needed(
        self,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
    ) -> str | None:
        if not self._should_auto_compact():
            return None
        return await self._acompact_impl(
            prompt=self.auto_compact_prompt, on_token=on_token, on_retry=on_retry
        )

    # -- Live snapshot helpers ----------------------------------------------

    def _live_reset(self) -> None:
        with self._live_lock:
            self._live_content_parts = []
            self._live_thinking_parts = []
            self._live_tool_events = []
            self._live_in_progress = True
            now = time.monotonic()
            self._live_started_at = now
            self._live_updated_at = now

    def _live_append_content(self, text: str) -> None:
        if not text:
            return
        with self._live_lock:
            self._live_content_parts.append(text)
            self._live_updated_at = time.monotonic()

    def _live_append_thinking(self, text: str) -> None:
        if not text:
            return
        with self._live_lock:
            self._live_thinking_parts.append(text)
            self._live_updated_at = time.monotonic()

    def _live_append_tool(self, event: str) -> None:
        with self._live_lock:
            self._live_tool_events.append(event)
            self._live_updated_at = time.monotonic()

    def _live_finish(self) -> None:
        with self._live_lock:
            self._live_in_progress = False
            self._live_updated_at = time.monotonic()

    def capture(self) -> LiveSnapshot:
        """Return a thread-safe snapshot of the current/last streaming turn."""
        with self._live_lock:
            return LiveSnapshot(
                content="".join(self._live_content_parts),
                thinking="".join(self._live_thinking_parts),
                tool_events=list(self._live_tool_events),
                in_progress=self._live_in_progress,
                started_at=self._live_started_at,
                updated_at=self._live_updated_at,
            )

    def interrupt(self) -> None:
        """Request the current streaming turn to stop as soon as possible."""
        with self._turn_state_lock:
            turn = self._active_turn
        if turn is not None:
            turn.cancel()

    def interrupted(self) -> bool:
        with self._turn_state_lock:
            turn = self._active_turn
            return turn.interrupted if turn is not None else self._last_turn_interrupted

    def _begin_turn(self) -> TurnContext:
        if not self._turn_lock.acquire(blocking=False):
            raise RuntimeError("another turn is already running")
        turn = TurnContext(turn_id=uuid4().hex, started_at=time.monotonic())
        with self._turn_state_lock:
            self._active_turn = turn
            self._last_turn_interrupted = False
        return turn

    def _end_turn(self, turn: TurnContext) -> None:
        with self._turn_state_lock:
            self._last_turn_interrupted = turn.interrupted
            if self._active_turn is turn:
                self._active_turn = None
        self._turn_lock.release()

    @staticmethod
    def _should_stop(
        turn: TurnContext,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        if should_stop and should_stop():
            turn.cancel()
        return turn.interrupted

    @staticmethod
    def _start_stop_watcher(
        turn: TurnContext,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[threading.Event, threading.Thread] | None:
        """Bridge an external stop callback into the turn cancellation event.

        ``should_stop`` is normally polled between streamed LLM chunks. A
        synchronous tool call can run for much longer than that interval, so
        polling must continue while the tool is executing as well. Built-in
        tools such as ``run_command`` receive ``turn.cancel_event`` and can
        terminate their child process as soon as this watcher cancels it.
        """
        if should_stop is None:
            return None
        stopped = threading.Event()

        def _watch() -> None:
            while not stopped.wait(0.05):
                try:
                    if should_stop():
                        turn.cancel()
                        return
                except Exception:
                    # A stop callback must never make the model turn fail.
                    return

        watcher = threading.Thread(
            target=_watch,
            name="enkiball-stop-watcher",
            daemon=True,
        )
        watcher.start()
        return stopped, watcher

    @staticmethod
    async def _watch_async_stop(
        turn: TurnContext,
        should_stop: Callable[[], bool],
    ) -> None:
        """Async counterpart of :meth:`_start_stop_watcher`."""
        while True:
            await asyncio.sleep(0.05)
            try:
                if should_stop():
                    turn.cancel()
                    return
            except Exception:
                return

    @staticmethod
    def _retry_delay(attempt: int) -> int:
        idx = min(max(attempt, 1), len(REQUEST_RETRY_DELAYS)) - 1
        return REQUEST_RETRY_DELAYS[idx]

    def _wait_for_retry(
        self,
        delay_seconds: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            time.sleep(min(0.2, deadline - time.monotonic()))
        return True

    async def _await_retry(
        self,
        delay_seconds: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            await asyncio.sleep(min(0.2, deadline - time.monotonic()))
        return True

    def _retry_event(self, attempt: int, delay_seconds: int, exc: Exception) -> RetryEvent:
        return RetryEvent(
            attempt=attempt,
            max_retries=MAX_REQUEST_RETRIES,
            delay_seconds=delay_seconds,
            next_retry_at=time.time() + delay_seconds,
            error=str(exc),
        )

    @property
    def model(self) -> str:
        return self._model_override or get_active_model(self.config, self.state_path, state=self.state)

    @model.setter
    def model(self, new_model: str) -> None:
        # Explicit runtime switching keeps the historical persistent behavior.
        # Constructor-level overrides (used by isolated solver roles) avoid
        # touching the shared active-model state.
        self._model_override = None
        set_active_model(self.config, new_model, self.state_path, state=self.state)

    def _client(self) -> OpenAI:
        api_key, base_url = resolve_provider_credentials(self.config, self.state_path, state=self.state)
        if not api_key:
            raise RuntimeError(
                "Missing API key. Set the appropriate env var or configure "
                "api_key_env in your provider config."
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        turn = self._active_turn
        transport = InterruptibleTransport(turn.cancel_event if turn else threading.Event())
        return OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(
            transport=transport, trust_env=False, timeout=httpx.Timeout(600, connect=5),
        ))

    def _async_client(self) -> AsyncOpenAI:
        api_key, base_url = resolve_provider_credentials(self.config, self.state_path, state=self.state)
        if not api_key:
            raise RuntimeError(
                "Missing API key. Configure api_key in your provider config."
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        turn = self._active_turn
        transport = InterruptibleAsyncTransport(turn.cancel_event if turn else threading.Event())
        return AsyncOpenAI(**kwargs, max_retries=0, http_client=httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=httpx.Timeout(600, connect=5),
        ))

    def _get_tools(self) -> list[dict[str, Any]]:
        tools = list(TOOL_SCHEMAS)
        tools.extend(copy.deepcopy(list(self._internal_tools.values())))
        tools.extend(self._custom_tool_schemas)
        if self.mcp_manager:
            tools.extend(self.mcp_manager.get_tools())
        return tools

    @staticmethod
    def _tool_schema_name(schema: dict[str, Any], fallback: str = "") -> str:
        if schema.get("type") == "function":
            fn_block = schema.get("function", {})
            if isinstance(fn_block, dict):
                return str(fn_block.get("name", fallback))
        return fallback

    def register_tool(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> str:
        tool_name = self._tool_schema_name(schema, name)
        if not tool_name:
            raise ValueError("tool schema must define a non-empty function name")
        if schema.get("type") != "function" or not isinstance(
            schema.get("function"), dict
        ):
            raise ValueError("tool schema must be an OpenAI function schema")
        parameters = schema["function"].get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        builtins = {self._tool_schema_name(item) for item in TOOL_SCHEMAS}
        if tool_name in builtins or tool_name.startswith("mcp__"):
            raise ValueError(f"reserved tool name: {tool_name}")
        if tool_name in self._internal_tool_handlers:
            raise ValueError(f"reserved tool name: {tool_name}")
        if tool_name in self._tool_handlers or any(
            self._tool_schema_name(item) == tool_name
            for item in self._custom_tool_schemas
        ):
            raise ValueError(f"tool already registered: {tool_name}")
        self._custom_tool_schemas.append(copy.deepcopy(schema))
        self._tool_handlers[tool_name] = handler
        return tool_name

    def set_internal_tool(
        self,
        name: str,
        schema: dict[str, Any] | None,
        handler: Callable[[dict[str, Any]], Any] | None,
    ) -> None:
        self._internal_tools.pop(name, None)
        self._internal_tool_handlers.pop(name, None)
        if schema is None or handler is None:
            return
        tool_name = self._tool_schema_name(schema, name)
        if tool_name != name:
            raise ValueError("internal tool schema name does not match")
        if schema.get("type") != "function" or not isinstance(
            schema.get("function"), dict
        ):
            raise ValueError("tool schema must be an OpenAI function schema")
        parameters = schema["function"].get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        builtins = {self._tool_schema_name(item) for item in TOOL_SCHEMAS}
        if name in builtins or name.startswith("mcp__") or name in self._tool_handlers:
            raise ValueError(f"reserved tool name: {name}")
        self._internal_tools[name] = copy.deepcopy(schema)
        self._internal_tool_handlers[name] = handler

    def unregister_tool(self, name: str) -> bool:
        existed = name in self._tool_handlers
        self._tool_handlers.pop(name, None)
        self._custom_tool_schemas = [
            schema
            for schema in self._custom_tool_schemas
            if self._tool_schema_name(schema) != name
        ]
        return existed

    def list_custom_tools(self) -> list[str]:
        return list(self._tool_handlers.keys())

    @property
    def system_prompt(self) -> str:
        first = self.messages[0] if self.messages else {}
        return str(first.get("content", self._system_prompt))

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = prompt
            return
        self.messages.insert(0, {"role": "system", "content": prompt})

    def reset_messages(self) -> None:
        self.messages = default_messages(self._system_prompt)
        self.compact_index = 0

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        loaded = copy.deepcopy(messages)
        if (
            loaded
            and isinstance(loaded[0], dict)
            and loaded[0].get("role") == "system"
        ):
            self.messages = loaded
        else:
            self.messages = default_messages(self._system_prompt) + loaded
        # Scan for the last compact marker to restore compact_index
        self.compact_index = 0
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "system" and msg.get("_compact"):
                self.compact_index = i

    def export_messages(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.messages)

    @staticmethod
    def _persist_mcp_image(base64_data: str, mime_type: str | None = None) -> str:
        image_dir = Path.home() / ".config" / "enkiball" / "mcp_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        mime = mime_type or "application/octet-stream"
        ext = mimetypes.guess_extension(mime) or ".bin"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = image_dir / f"{ts}-{uuid4().hex[:8]}{ext}"
        raw = base64.b64decode(base64_data)
        target.write_bytes(raw)
        return str(target)

    def _prepare_messages_for_api(self) -> list[dict[str, Any]]:
        return prepare_messages_for_api(self.messages[self.compact_index :])

    @staticmethod
    def _prepare_history_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepare an arbitrary message list for API replay.

        Unlike _prepare_messages_for_api(), this is used for temporary message
        lists (compact) and preserves provider-required keys like
        reasoning_content.
        """
        return prepare_messages_for_api(messages)

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
            prefill, restored_images = extract_user_content(removed_user)
            reverted += 1
        if not self.messages:
            self.messages = default_messages(self._system_prompt)
        self.compact_index = 0
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "system" and msg.get("_compact"):
                self.compact_index = i
        return reverted, prefill, restored_images

    def fork_at_turn(self, user_turn_index: int) -> list[dict[str, Any]]:
        """Return messages up to and including the Nth user turn (1-based).

        Useful for creating a forked session from a specific point.
        """
        if user_turn_index < 1:
            return []
        count = 0
        cut = len(self.messages)
        found = False
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "user":
                count += 1
                if count == user_turn_index:
                    found = True
                    # Include this user turn and all subsequent assistant/tool
                    # messages up to the next user turn.
                    for j in range(i + 1, len(self.messages)):
                        if self.messages[j].get("role") == "user":
                            cut = j
                            break
                    else:
                        cut = len(self.messages)
                    break
        return copy.deepcopy(self.messages[:cut]) if found else []

    @staticmethod
    def _parse_tool_args(raw_args: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _flatten_mcp_content(mcp_res: Any) -> dict[str, Any]:
        if not isinstance(mcp_res, dict):
            return {"ok": True, "result": mcp_res}

        is_error = bool(mcp_res.get("isError"))
        payload: dict[str, Any] = {"ok": not is_error}
        for key, value in mcp_res.items():
            if key not in {"content", "isError"}:
                payload[key] = value

        content_list = mcp_res.get("content", [])
        texts: list[str] = []
        saved_images: list[str] = []
        other_content: list[Any] = []
        for item in content_list:
            if not isinstance(item, dict):
                other_content.append(item)
            elif item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif item.get("type") == "image":
                data = item.get("data")
                if isinstance(data, str) and data:
                    try:
                        path = ChatEngine._persist_mcp_image(data, item.get("mimeType"))
                        saved_images.append(path)
                        continue
                    except Exception:
                        pass
                other_content.append(
                    {key: value for key, value in item.items() if key != "data"}
                )
            else:
                other_content.append(item)
        if texts:
            payload["text"] = "\n".join(texts)
        if saved_images:
            payload["images"] = saved_images
        if other_content:
            payload["content"] = other_content
        return payload

    def _dispatch_tool(
        self,
        name: str,
        args: dict[str, Any],
        turn: TurnContext | None = None,
    ) -> str:
        result = self._dispatch_tool_raw(name, args, turn)
        return self._attach_sys_notices(result)

    def _dispatch_tool_raw(
        self,
        name: str,
        args: dict[str, Any],
        turn: TurnContext | None = None,
    ) -> str:
        handler = self._internal_tool_handlers.get(name) or self._tool_handlers.get(name)
        if handler is not None:
            try:
                result = handler(args)
                if isinstance(result, str):
                    return result
                # Allow returning dict/list — JSON-encode for the LLM
                return json.dumps(
                    {"ok": True, "result": result}, ensure_ascii=False, default=str
                )
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

        if not name.startswith("mcp__"):
            return dispatch_tool(
                name,
                args,
                self.sandbox,
                cancel_event=turn.cancel_event if turn is not None else None,
            )
        if not self.mcp_manager:
            return json.dumps(
                {"ok": False, "error": "MCP manager not initialized"},
                ensure_ascii=False,
            )
        try:
            mcp_res = self.mcp_manager.call_tool(name, args)
            return json.dumps(self._flatten_mcp_content(mcp_res), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    def _attach_sys_notices(self, tool_result: str) -> str:
        drain = getattr(self.notice_queue, "drain", None)
        if callable(drain):
            notices = drain()
        else:
            notices = list(self.notice_queue)
            self.notice_queue.clear()
        if not notices:
            return tool_result
        notice_block = (
            "<sys-notice>\n"
            "Delayed system notices supplied by the harness:\n"
            f"{json.dumps(notices, ensure_ascii=False, default=str)}\n"
            "</sys-notice>"
        )
        if not tool_result:
            return notice_block
        return f"{tool_result}\n\n{notice_block}"

    @staticmethod
    def _collect_stream_output(
        stream: Any,
        on_token: Callable[[str], None],
        on_thinking: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, str, list[ParsedToolCall], bool, TurnUsage]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        interrupted = False
        usage = TurnUsage()

        try:
            for event in stream:
                if should_stop and should_stop():
                    interrupted = True
                    break
                usage.update(event)
                if not event.choices:
                    continue

                delta = event.choices[0].delta

                # Handle thinking/reasoning content if available
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_thinking:
                        on_thinking(reasoning)

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
        except TurnInterrupted:
            interrupted = True
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
        return "".join(content_parts), "".join(reasoning_parts), tool_calls, interrupted, usage

    @staticmethod
    def _append_assistant_message(
        messages: list[dict[str, Any]],
        assistant_content: str,
        tool_calls: list[ParsedToolCall],
        reasoning_content: str = "",
    ) -> None:
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": assistant_content,
        }
        if reasoning_content:
            # Thinking-mode APIs (e.g. some OpenAI-compatible providers) require
            # the model's reasoning_content to be passed back on later turns.
            assistant_msg["reasoning_content"] = reasoning_content
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

    def _build_stream_request(self, llm_params: dict[str, Any]) -> dict[str, Any]:
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare_messages_for_api(),
            "temperature": llm_params.get("temperature", 1),
            "max_tokens": llm_params.get("max_tokens", DEFAULT_ENDPOINT_MAX_TOKENS),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tools = self._get_tools()
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "auto"
        if "extra_body" in llm_params:
            create_kwargs["extra_body"] = llm_params["extra_body"]
        return create_kwargs

    def _record_stream_response(
        self,
        assistant_content: str,
        reasoning_content: str,
        tool_calls: list[ParsedToolCall],
        interrupted: bool,
        usage: TurnUsage,
        turn: TurnContext,
        should_stop: Callable[[], bool] | None,
    ) -> Literal["interrupted", "complete", "tools"]:
        self.token_usage.add(usage.prompt_tokens, usage.completion_tokens)
        if interrupted or self._should_stop(turn, should_stop):
            if assistant_content or reasoning_content:
                self._append_assistant_message(
                    self.messages, assistant_content, [], reasoning_content
                )
                self.messages[-1]["_usage"] = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                }
            return "interrupted"

        self._append_assistant_message(
            self.messages, assistant_content, tool_calls, reasoning_content
        )
        self.messages[-1]["_usage"] = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        }
        return "tools" if tool_calls else "complete"

    def _record_interrupted_tool_calls(
        self, tool_calls: list[ParsedToolCall], start_index: int
    ) -> None:
        interrupted_result = json.dumps(
            {"ok": False, "error": "turn interrupted"}, ensure_ascii=False
        )
        for pending in tool_calls[start_index:]:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending.tool_call_id,
                    "content": interrupted_result,
                }
            )

    def _append_tool_result(self, tool_call_id: str, result: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": result}
        )

    def _tool_loop_fallback(self) -> str:
        fallback = "Tool-calling loop reached limit. Please refine the request."
        self.messages.append({"role": "assistant", "content": fallback})
        return fallback

    def _build_compact_request(self, prompt: str | None) -> dict[str, Any]:
        temp_msgs = self._prepare_history_for_api(
            self.messages[self.compact_index :]
        )
        temp_msgs.append({"role": "user", "content": prompt or DEFAULT_COMPACT_PROMPT})
        model_params = get_model_params(self.config, self.model, state_path=self.state_path, state=self.state)
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": temp_msgs,
            "temperature": model_params.get("temperature", 0.2),
            "max_tokens": model_params.get(
                "max_tokens", DEFAULT_ENDPOINT_MAX_TOKENS
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if "extra_body" in model_params:
            create_kwargs["extra_body"] = model_params["extra_body"]
        return create_kwargs

    def _commit_compact_summary(self, summary: str, usage: TurnUsage) -> None:
        self.token_usage.add(usage.prompt_tokens, usage.completion_tokens)
        compact_marker: dict[str, Any] = {
            "role": "system",
            "content": f"[Compact summary of prior conversation]\n\n{summary}",
            "_compact": True,
            "_usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        }
        self.messages.append(compact_marker)
        self.compact_index = len(self.messages) - 1

    def compact(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        turn = self._begin_turn()
        watcher = self._start_stop_watcher(turn, should_stop)
        try:
            return self._compact_impl(prompt, on_token, on_retry)
        finally:
            if watcher is not None:
                stopped, thread = watcher
                stopped.set()
                thread.join(timeout=0.2)
            self._end_turn(turn)

    def _compact_impl(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
    ) -> str:
        """Summarize conversation history and set compact boundary.

        Messages before the boundary are preserved in ``self.messages`` (for
        session persistence) but excluded from future LLM calls.

        Returns the summary text.
        """
        create_kwargs = self._build_compact_request(prompt)
        client = self._client()
        for attempt in range(MAX_REQUEST_RETRIES + 1):
            summary_parts: list[str] = []
            usage = TurnUsage()
            try:
                stream: Any = client.chat.completions.create(**create_kwargs)
                try:
                    for event in stream:
                        usage.update(event)
                        if not event.choices:
                            continue
                        delta = event.choices[0].delta
                        if delta.content:
                            summary_parts.append(delta.content)
                            if on_token:
                                on_token(delta.content)
                finally:
                    close_fn = getattr(stream, "close", None)
                    if callable(close_fn):
                        close_fn()
                break
            except Exception as exc:
                if self._active_turn is not None and self._active_turn.interrupted:
                    client.close()
                    raise TurnInterrupted from exc
                if summary_parts or attempt >= MAX_REQUEST_RETRIES:
                    client.close()
                    raise
                delay = self._retry_delay(attempt + 1)
                if on_retry:
                    on_retry(self._retry_event(attempt + 1, delay, exc))
                if not self._wait_for_retry(
                    delay, should_stop=lambda: self.interrupted()
                ):
                    client.close()
                    raise TurnInterrupted

        client.close()
        summary = "".join(summary_parts)
        self._commit_compact_summary(summary, usage)
        return summary

    def stream_reply(
        self,
        user_input: str,
        image_paths: list[str] | None,
        on_token: Callable[[str], None],
        on_tool_event: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        turn = self._begin_turn()
        watcher = self._start_stop_watcher(turn, should_stop)
        try:
            return self._stream_reply_impl(
                user_input,
                image_paths,
                on_token,
                on_tool_event,
                on_thinking,
                on_retry,
                system_context,
                should_stop,
                turn,
            )
        finally:
            if watcher is not None:
                stopped, thread = watcher
                stopped.set()
                thread.join(timeout=0.2)
            self._end_turn(turn)

    def _stream_reply_impl(
        self,
        user_input: str,
        image_paths: list[str] | None,
        on_token: Callable[[str], None],
        on_tool_event: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        turn: TurnContext | None = None,
    ) -> str:
        if turn is None:
            raise RuntimeError("turn context is required")
        content = build_user_content(user_input, image_paths or [])
        if system_context:
            self.messages.append({"role": "system", "content": system_context})
        self.messages.append({"role": "user", "content": content})

        llm_params = get_model_params(self.config, self.model, state_path=self.state_path, state=self.state)
        client = self._client()

        self._live_reset()

        def _tok(t: str) -> None:
            self._live_append_content(t)
            on_token(t)

        def _think(t: str) -> None:
            self._live_append_thinking(t)
            if on_thinking:
                on_thinking(t)

        def _tool(ev: str) -> None:
            self._live_append_tool(ev)
            if on_tool_event:
                on_tool_event(ev)

        try:
            for _ in range(TOOL_CALL_LIMIT):
                if self._should_stop(turn, should_stop):
                    return ""
                create_kwargs = self._build_stream_request(llm_params)

                for attempt in range(MAX_REQUEST_RETRIES + 1):
                    emitted_before = self.capture()
                    try:
                        stream: Any = client.chat.completions.create(**create_kwargs)
                        (
                            assistant_content,
                            reasoning_content,
                            tool_calls,
                            interrupted,
                            turn_usage,
                        ) = self._collect_stream_output(
                            stream,
                            _tok,
                            on_thinking=_think,
                            should_stop=lambda: self._should_stop(turn, should_stop),
                        )
                        break
                    except Exception as exc:
                        if self._should_stop(turn, should_stop):
                            return ""
                        emitted_after = self.capture()
                        emitted = (
                            emitted_after.content != emitted_before.content
                            or emitted_after.thinking != emitted_before.thinking
                        )
                        if emitted or attempt >= MAX_REQUEST_RETRIES:
                            raise
                        delay = self._retry_delay(attempt + 1)
                        if on_retry:
                            on_retry(self._retry_event(attempt + 1, delay, exc))
                        if not self._wait_for_retry(
                            delay,
                            should_stop=lambda: self._should_stop(turn, should_stop),
                        ):
                            return ""
                response_state = self._record_stream_response(
                    assistant_content,
                    reasoning_content,
                    tool_calls,
                    interrupted,
                    turn_usage,
                    turn,
                    should_stop,
                )
                if response_state == "interrupted":
                    return assistant_content
                if response_state == "complete":
                    self._auto_compact_if_needed(on_retry=on_retry)
                    return assistant_content

                for index, tc in enumerate(tool_calls):
                    args = self._parse_tool_args(tc.arguments_raw)
                    _tool(f"{tc.name}({tc.arguments_raw})")
                    try:
                        if self._should_stop(turn, should_stop):
                            raise TurnInterrupted
                        result = self._dispatch_tool(tc.name, args, turn)
                    except TurnInterrupted:
                        self._record_interrupted_tool_calls(tool_calls, index)
                        if turn.interrupted:
                            return assistant_content
                        raise
                    self._append_tool_result(tc.tool_call_id, result)

            fallback = self._tool_loop_fallback()
            _tok(fallback)
            return fallback
        finally:
            self._live_finish()
            client.close()

    # ========================================================================
    # Async variants
    # ========================================================================

    async def _adispatch_tool(
        self, name: str, args: dict[str, Any], turn: TurnContext
    ) -> str:
        """Async tool dispatch.  Local tools run in a thread pool to avoid
        blocking the event loop; MCP calls run via to_thread for now (the MCP
        client is sync)."""
        task = asyncio.create_task(
            asyncio.to_thread(self._dispatch_tool, name, args, turn)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            turn.cancel()
            try:
                await asyncio.shield(task)
            except TurnInterrupted:
                pass
            raise

    @staticmethod
    async def _acollect_stream_output(
        stream: Any,
        on_token: Callable[[str], Awaitable[None] | None],
        on_thinking: Callable[[str], Awaitable[None] | None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, str, list[ParsedToolCall], bool, TurnUsage]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        interrupted = False
        usage = TurnUsage()

        async def _maybe_await(cb: Callable[[str], Any] | None, val: str) -> None:
            if cb is None:
                return
            res = cb(val)
            if asyncio.iscoroutine(res):
                await res

        try:
            async for event in stream:
                if should_stop and should_stop():
                    interrupted = True
                    break
                usage.update(event)
                if not event.choices:
                    continue

                delta = event.choices[0].delta

                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    reasoning_parts.append(reasoning)
                    await _maybe_await(on_thinking, reasoning)

                if delta.content:
                    content_parts.append(delta.content)
                    await _maybe_await(on_token, delta.content)

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
        except TurnInterrupted:
            interrupted = True
        finally:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    try:
                        await res
                    except Exception:
                        pass

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
        return "".join(content_parts), "".join(reasoning_parts), tool_calls, interrupted, usage

    async def acompact(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        turn = self._begin_turn()
        stop_task = (
            asyncio.create_task(self._watch_async_stop(turn, should_stop))
            if should_stop is not None else None
        )
        try:
            return await self._acompact_impl(prompt, on_token, on_retry)
        except asyncio.CancelledError:
            turn.cancel()
            raise
        finally:
            if stop_task is not None:
                stop_task.cancel()
                try:
                    await stop_task
                except asyncio.CancelledError:
                    pass
            self._end_turn(turn)

    async def _acompact_impl(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
    ) -> str:
        """Async version of :meth:`compact`."""
        create_kwargs = self._build_compact_request(prompt)
        client = self._async_client()
        async def _maybe_retry(cb: Callable[[RetryEvent], Any] | None, event: RetryEvent) -> None:
            if cb is None:
                return
            res = cb(event)
            if asyncio.iscoroutine(res):
                await res

        for attempt in range(MAX_REQUEST_RETRIES + 1):
            summary_parts: list[str] = []
            usage = TurnUsage()
            try:
                stream: Any = await client.chat.completions.create(**create_kwargs)
                try:
                    async for event in stream:
                        usage.update(event)
                        if not event.choices:
                            continue
                        delta = event.choices[0].delta
                        if delta.content:
                            summary_parts.append(delta.content)
                            if on_token is not None:
                                res = on_token(delta.content)
                                if asyncio.iscoroutine(res):
                                    await res
                finally:
                    close_fn = getattr(stream, "close", None)
                    if callable(close_fn):
                        res = close_fn()
                        if asyncio.iscoroutine(res):
                            try:
                                await res
                            except Exception:
                                pass
                break
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception as exc:
                if self._active_turn is not None and self._active_turn.interrupted:
                    await client.close()
                    raise TurnInterrupted from exc
                if summary_parts or attempt >= MAX_REQUEST_RETRIES:
                    await client.close()
                    raise
                delay = self._retry_delay(attempt + 1)
                await _maybe_retry(on_retry, self._retry_event(attempt + 1, delay, exc))
                if not await self._await_retry(delay, should_stop=self.interrupted):
                    await client.close()
                    raise TurnInterrupted

        await client.close()
        summary = "".join(summary_parts)
        self._commit_compact_summary(summary, usage)
        return summary

    async def astream_reply(
        self,
        user_input: str,
        image_paths: list[str] | None,
        on_token: Callable[[str], Awaitable[None] | None],
        on_tool_event: Callable[[str], Awaitable[None] | None] | None = None,
        on_thinking: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        turn = self._begin_turn()
        stop_task: asyncio.Task[None] | None = None
        if should_stop is not None:
            stop_task = asyncio.create_task(self._watch_async_stop(turn, should_stop))
        try:
            return await self._astream_reply_impl(
                user_input,
                image_paths,
                on_token,
                on_tool_event,
                on_thinking,
                on_retry,
                system_context,
                should_stop,
                turn,
            )
        except asyncio.CancelledError:
            turn.cancel()
            raise
        finally:
            if stop_task is not None:
                stop_task.cancel()
                try:
                    await stop_task
                except asyncio.CancelledError:
                    pass
            self._end_turn(turn)

    async def _astream_reply_impl(
        self,
        user_input: str,
        image_paths: list[str] | None,
        on_token: Callable[[str], Awaitable[None] | None],
        on_tool_event: Callable[[str], Awaitable[None] | None] | None = None,
        on_thinking: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        turn: TurnContext | None = None,
    ) -> str:
        """Async counterpart of :meth:`stream_reply`.  Callbacks may be
        either plain functions or coroutine functions."""
        if turn is None:
            raise RuntimeError("turn context is required")
        content = build_user_content(user_input, image_paths or [])
        if system_context:
            self.messages.append({"role": "system", "content": system_context})
        self.messages.append({"role": "user", "content": content})

        llm_params = get_model_params(self.config, self.model, state_path=self.state_path, state=self.state)
        client = self._async_client()

        self._live_reset()

        async def _maybe_await(cb: Callable[[str], Any] | None, val: str) -> None:
            if cb is None:
                return
            res = cb(val)
            if asyncio.iscoroutine(res):
                await res

        async def _tok(t: str) -> None:
            self._live_append_content(t)
            await _maybe_await(on_token, t)

        async def _think(t: str) -> None:
            self._live_append_thinking(t)
            await _maybe_await(on_thinking, t)

        async def _tool(ev: str) -> None:
            self._live_append_tool(ev)
            await _maybe_await(on_tool_event, ev)

        try:
            for _ in range(TOOL_CALL_LIMIT):
                if self._should_stop(turn, should_stop):
                    return ""
                create_kwargs = self._build_stream_request(llm_params)

                for attempt in range(MAX_REQUEST_RETRIES + 1):
                    emitted_before = self.capture()
                    try:
                        stream: Any = await client.chat.completions.create(**create_kwargs)
                        (
                            assistant_content,
                            reasoning_content,
                            tool_calls,
                            interrupted,
                            turn_usage,
                        ) = await self._acollect_stream_output(
                            stream,
                            _tok,
                            on_thinking=_think,
                            should_stop=lambda: self._should_stop(turn, should_stop),
                        )
                        break
                    except Exception as exc:
                        if self._should_stop(turn, should_stop):
                            return ""
                        emitted_after = self.capture()
                        emitted = (
                            emitted_after.content != emitted_before.content
                            or emitted_after.thinking != emitted_before.thinking
                        )
                        if emitted or attempt >= MAX_REQUEST_RETRIES:
                            raise
                        delay = self._retry_delay(attempt + 1)
                        await _maybe_await(
                            on_retry,
                            self._retry_event(attempt + 1, delay, exc),
                        )
                        if not await self._await_retry(
                            delay,
                            should_stop=lambda: self._should_stop(turn, should_stop),
                        ):
                            return ""
                response_state = self._record_stream_response(
                    assistant_content,
                    reasoning_content,
                    tool_calls,
                    interrupted,
                    turn_usage,
                    turn,
                    should_stop,
                )
                if response_state == "interrupted":
                    return assistant_content
                if response_state == "complete":
                    await self._aauto_compact_if_needed(on_retry=on_retry)
                    return assistant_content

                for index, tc in enumerate(tool_calls):
                    args = self._parse_tool_args(tc.arguments_raw)
                    await _tool(f"{tc.name}({tc.arguments_raw})")
                    try:
                        if self._should_stop(turn, should_stop):
                            raise TurnInterrupted
                        result = await self._adispatch_tool(tc.name, args, turn)
                    except (TurnInterrupted, asyncio.CancelledError) as exc:
                        self._record_interrupted_tool_calls(tool_calls, index)
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        if turn.interrupted:
                            return assistant_content
                        raise
                    self._append_tool_result(tc.tool_call_id, result)

            fallback = self._tool_loop_fallback()
            await _tok(fallback)
            return fallback
        finally:
            self._live_finish()
            await client.close()
