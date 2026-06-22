"""Enkiball programmatic API.

Usage::

    from enkiball import Enkiball

    with Enkiball() as agent:
        # Basic chat
        result = agent.chat("scan open ports on localhost")
        print(result)

        # Streaming
        for token in agent.stream("explain the results"):
            print(token, end="", flush=True)

        # With images
        result = agent.chat("what is in this screenshot?", images=["screen.png"])

        # Provider / model management
        print(agent.providers)
        agent.switch_provider("openai")
        agent.model = "gpt-4o"

        # MCP control
        agent.mcp_start("ssh-mcp")
        agent.mcp_stop("ssh-mcp")
        agent.mcp_enable("ssh-mcp")

        # Token stats
        print(agent.token_usage)

        # Thinking output
        agent.show_thinking = True

        # Session fork
        forked = agent.fork_at_turn(2)

        # History
        print(agent.history())
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Generator

from .config import (
    DEFAULT_CONFIG_PATH,
    ensure_config,
    get_active_model,
    get_active_provider,
    get_provider_models,
    get_providers,
    load_config,
    save_config,
    set_active_model,
    set_active_provider,
    set_mcp_enabled,
    list_mcp_servers,
    load_state,
    save_state,
)
from .llm import (
    ChatEngine,
    DEFAULT_COMPACT_PROMPT,
    LiveSnapshot,
    RetryEvent,
    SYSTEM_PROMPT,
    TokenUsage,
)
from .mcp import MCPManager
from .session_store import SessionRecord, SessionStore


@dataclass
class ChatResult:
    """Structured result from a single chat turn."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str = ""
    interrupted: bool = False

    def __str__(self) -> str:
        return self.content


class Enkiball:
    """High-level programmatic interface to the Enkiball agent.

    Parameters
    ----------
    config_path : str | Path
        Path to ``agent.json``.  Defaults to ``config/agent.json``.
    system_prompt : str | None
        Override the default system prompt.
    auto_start_mcp : bool
        If *True* (default), enabled MCP servers are started on init.
    extra_tools : list[dict] | None
        Extra OpenAI-style tool schemas to register alongside built-ins.
    on_tool_event : callable | None
        Optional callback ``(event_str) -> None`` fired before each tool
        execution.  Useful for logging / auditing.
    """

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        system_prompt: str | None = None,
        auto_start_mcp: bool = True,
        extra_tools: list[dict[str, Any]] | None = None,
        on_tool_event: Callable[[str], None] | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        ensure_config(self._config_path)
        self._config = load_config(self._config_path)

        self._mcp = MCPManager(self._config)
        self._engine = ChatEngine(self._config, self._mcp)
        self._on_tool_event = on_tool_event
        self._session_store: SessionStore | None = None

        if extra_tools:
            self._engine._extra_tools = extra_tools

        if system_prompt is not None:
            self._engine.messages[0]["content"] = system_prompt

        if auto_start_mcp:
            self._mcp.start_enabled()

    # ========================================================================
    # Custom tools
    # ========================================================================

    def register_tool(
        self,
        func: Callable[[dict[str, Any]], Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Callable[[dict[str, Any]], Any]:
        """Register a custom tool the LLM can call.

        Two equivalent forms::

            # 1) Pass schema explicitly
            agent.register_tool(
                my_func,
                name="search",
                description="Search the web",
                parameters={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            )

            # 2) Pass a complete OpenAI tool schema
            agent.register_tool(my_func, schema={"type": "function", "function": {...}})

        ``func`` signature: ``(args: dict) -> str | dict | list``.  Strings are
        passed straight through to the LLM; other types are JSON-encoded.

        Can also be used as a decorator::

            @agent.register_tool(name="ping", description="Ping a host",
                                 parameters={"type": "object",
                                             "properties": {"host": {"type": "string"}},
                                             "required": ["host"]})
            def ping(args):
                return subprocess.check_output(["ping", "-c1", args["host"]]).decode()
        """

        def _do_register(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
            tool_name = name or fn.__name__
            if schema is not None:
                full_schema = schema
                # Normalize and extract canonical name
                if full_schema.get("type") == "function":
                    fn_block = full_schema.get("function", {})
                    tool_name_eff = fn_block.get("name", tool_name)
                else:
                    tool_name_eff = tool_name
            else:
                params = parameters or {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }
                full_schema = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": description or (fn.__doc__ or "").strip()
                        or f"Custom tool: {tool_name}",
                        "parameters": params,
                    },
                }
                tool_name_eff = tool_name

            # Replace any existing schema/handler with the same name
            self._engine._extra_tools = [
                s
                for s in self._engine._extra_tools
                if s.get("function", {}).get("name") != tool_name_eff
            ]
            self._engine._extra_tools.append(full_schema)
            self._engine._tool_handlers[tool_name_eff] = fn  # type: ignore[assignment]
            return fn

        if func is None:
            # Used as decorator with kwargs
            return _do_register  # type: ignore[return-value]
        return _do_register(func)

    def unregister_tool(self, name: str) -> bool:
        """Remove a previously registered custom tool.  Returns True if found."""
        existed = name in self._engine._tool_handlers
        self._engine._tool_handlers.pop(name, None)
        self._engine._extra_tools = [
            s
            for s in self._engine._extra_tools
            if s.get("function", {}).get("name") != name
        ]
        return existed

    def list_custom_tools(self) -> list[str]:
        """Return names of registered custom tools."""
        return list(self._engine._tool_handlers.keys())

    # ========================================================================
    # Configuration & Provider management
    # ========================================================================

    @property
    def config(self) -> dict[str, Any]:
        """Return a *copy* of the active configuration."""
        return json.loads(json.dumps(self._config))

    @property
    def model(self) -> str:
        return self._engine.model

    @model.setter
    def model(self, name: str) -> None:
        self._engine.model = name

    @property
    def provider(self) -> str:
        """Name of the active provider."""
        return get_active_provider(self._config)

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        """All configured providers and their settings (secrets stripped)."""
        return get_providers(self._config)

    def switch_provider(self, name: str, save: bool = True) -> bool:
        """Switch to a different provider.  Returns False if not found.

        The selection is always persisted to ``state.json``.
        """
        return set_active_provider(self._config, name)

    def switch_model(self, model: str, save: bool = True) -> None:
        """Switch model (within current provider).

        The selection is always persisted to ``state.json``.
        """
        set_active_model(self._config, model)

    def list_models(
        self,
        provider_name: str | None = None,
        *,
        remote: bool = True,
        timeout: float = 5.0,
    ) -> list[str]:
        """Return merged model list (config + remote) for a provider.

        Config-defined models appear first, then remote-only models sorted.
        """
        return get_provider_models(
            self._config,
            provider_name=provider_name,
            remote=remote,
            timeout=timeout,
        )

    def refresh_models(self, timeout: float = 5.0) -> list[str]:
        """Force-fetch remote models for the active provider and return the merged list."""
        return self.list_models(remote=True, timeout=timeout)

    @property
    def system_prompt(self) -> str:
        first = self._engine.messages[0] if self._engine.messages else {}
        return str(first.get("content", ""))

    @system_prompt.setter
    def system_prompt(self, prompt: str) -> None:
        if self._engine.messages and self._engine.messages[0].get("role") == "system":
            self._engine.messages[0]["content"] = prompt
        else:
            self._engine.messages.insert(0, {"role": "system", "content": prompt})

    # ========================================================================
    # Thinking output toggle
    # ========================================================================

    @property
    def show_thinking(self) -> bool:
        return self._engine.show_thinking

    @show_thinking.setter
    def show_thinking(self, value: bool) -> None:
        self._engine.show_thinking = value

    # ========================================================================
    # Token statistics
    # ========================================================================

    @property
    def token_usage(self) -> TokenUsage:
        """Accumulated token usage across all turns."""
        return self._engine.token_usage

    def reset_token_usage(self) -> None:
        self._engine.token_usage = TokenUsage()

    @property
    def auto_compact_enabled(self) -> bool:
        """Whether automatic compaction is enabled for this agent."""
        return self._engine.auto_compact_enabled

    @auto_compact_enabled.setter
    def auto_compact_enabled(self, value: bool) -> None:
        self._engine.auto_compact_enabled = bool(value)

    @property
    def auto_compact_threshold_ratio(self) -> float:
        """Fraction of model max_tokens that triggers auto compact."""
        return self._engine.auto_compact_threshold_ratio

    @auto_compact_threshold_ratio.setter
    def auto_compact_threshold_ratio(self, value: float) -> None:
        self._engine.auto_compact_threshold_ratio = float(value)
        self._engine.auto_compact_threshold_tokens = None

    @property
    def auto_compact_effective_threshold_tokens(self) -> int:
        """Current concrete token threshold derived from model max_tokens."""
        return self._engine._auto_compact_effective_threshold_tokens()

    # ========================================================================
    # Compact
    # ========================================================================

    def compact(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
    ) -> str:
        """Summarize prior conversation and set a compact boundary.

        Messages before the boundary are kept in history (visible in session
        replay) but excluded from future LLM API calls.

        Parameters
        ----------
        prompt : str | None
            Custom compact/summarization prompt.  If *None*, the built-in
            ``DEFAULT_COMPACT_PROMPT`` is used.
        on_token : callable | None
            Optional streaming callback for summary tokens.

        Returns the summary text.
        """
        return self._engine.compact(prompt=prompt, on_token=on_token, on_retry=on_retry)

    # ========================================================================
    # Live capture (thread-safe snapshot of in-flight streaming turn)
    # ========================================================================

    def capture(self) -> LiveSnapshot:
        """Return a snapshot of the current (or most-recent) streaming turn.

        This method is **synchronous and thread-safe** — safe to call from any
        thread, including while :meth:`chat`, :meth:`stream`, :meth:`achat`,
        :meth:`astream`, or :meth:`astream_events` is still in progress.

        The returned :class:`LiveSnapshot` contains the content, thinking text,
        and tool events accumulated so far, plus a ``in_progress`` flag.

        Example::

            # Another thread / coroutine kicks off a long chat
            threading.Thread(target=lambda: agent.chat("big task")).start()

            # Poll progress from the main thread
            while True:
                snap = agent.capture()
                print(f"[{len(snap.content)} chars so far]")
                if not snap.in_progress:
                    break
                time.sleep(0.5)
        """
        return self._engine.capture()

    def interrupt(self) -> None:
        """Ask the currently running turn to stop as soon as possible.

        This is intentionally lightweight: it only sets an internal stop flag.
        It does not wait for the worker thread/task, drain resources, or shut
        down MCP processes.  The stream loop observes the flag between chunks
        and closes the OpenAI stream in its existing cleanup path.
        """
        self._engine.interrupt()

    cancel = interrupt

    @property
    def interrupted(self) -> bool:
        """Whether an interrupt has been requested for the current turn."""
        return self._engine.interrupted()

    # ========================================================================
    # Background execution (fire-and-poll pattern)
    # ========================================================================

    def chat_in_background(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> concurrent.futures.Future[ChatResult]:
        """Start :meth:`chat` in a background thread and return a Future.

        Use this together with :meth:`capture` for fire-and-poll workflows::

            fut = agent.chat_in_background("long task")
            while not fut.done():
                print(agent.capture().content)
                time.sleep(0.5)
            result = fut.result()   # ChatResult

        Only one background turn should be in flight at a time, since the
        underlying ``ChatEngine`` is single-conversation.
        """
        executor = self._bg_executor()
        return executor.submit(
            self.chat,
            message,
            images=images,
            system_context=system_context,
            should_stop=should_stop,
        )

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the current streaming turn finishes (or timeout).

        Returns *True* if it finished, *False* if timeout elapsed.  Polls
        :meth:`capture` internally.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            snap = self._engine.capture()
            if not snap.in_progress:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _bg_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        ex = getattr(self, "_bg_exec", None)
        if ex is None:
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="enkiball-bg"
            )
            self._bg_exec = ex
        return ex

    # ========================================================================
    # Core conversation
    # ========================================================================

    def chat(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResult:
        """Send *message* and block until the full reply is collected."""
        tool_calls_log: list[dict[str, Any]] = []
        thinking_parts: list[str] = []

        def _on_tool(event: str) -> None:
            tool_calls_log.append({"raw": event})
            if self._on_tool_event:
                self._on_tool_event(event)

        def _on_thinking(text: str) -> None:
            thinking_parts.append(text)

        content = self._engine.stream_reply(
            user_input=message,
            image_paths=images,
            on_token=lambda _: None,
            on_tool_event=_on_tool,
            on_thinking=_on_thinking,
            on_retry=on_retry,
            system_context=system_context,
            should_stop=should_stop,
        )
        interrupted = should_stop() if should_stop else False
        return ChatResult(
            content=content,
            tool_calls=tool_calls_log,
            thinking="".join(thinking_parts),
            interrupted=interrupted,
        )

    def stream(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Generator[str, None, ChatResult]:
        """Send *message* and yield tokens as they arrive.

        The generator's return value (``StopIteration.value``) is a
        :class:`ChatResult`.
        """
        tokens: list[str] = []
        tool_calls_log: list[dict[str, Any]] = []
        thinking_parts: list[str] = []
        _q: queue.Queue[str] = queue.Queue()
        _sentinel = object()

        def _on_token(tok: str) -> None:
            _q.put(tok)
            tokens.append(tok)

        def _on_tool(event: str) -> None:
            tool_calls_log.append({"raw": event})
            if self._on_tool_event:
                self._on_tool_event(event)

        def _on_thinking(text: str) -> None:
            thinking_parts.append(text)

        result_box: list[str] = []
        exc_box: list[Exception] = []
        interrupted_box: list[bool] = [False]

        def _worker() -> None:
            try:
                r = self._engine.stream_reply(
                    user_input=message,
                    image_paths=images,
                    on_token=_on_token,
                    on_tool_event=_on_tool,
                    on_thinking=_on_thinking,
                    on_retry=on_retry,
                    system_context=system_context,
                    should_stop=should_stop,
                )
                result_box.append(r)
                interrupted_box[0] = bool(should_stop and should_stop())
            except Exception as e:
                exc_box.append(e)
            finally:
                _q.put(_sentinel)  # type: ignore[arg-type]

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        while True:
            item = _q.get()
            if item is _sentinel:
                break
            yield item  # type: ignore[misc]

        t.join()
        if exc_box:
            raise exc_box[0]

        content = result_box[0] if result_box else "".join(tokens)
        return ChatResult(
            content=content,
            tool_calls=tool_calls_log,
            thinking="".join(thinking_parts),
            interrupted=interrupted_box[0],
        )

    # ========================================================================
    # Async conversation API
    # ========================================================================

    async def achat(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResult:
        """Async version of :meth:`chat`.  Collects the full reply then returns."""
        tool_calls_log: list[dict[str, Any]] = []
        thinking_parts: list[str] = []

        def _on_tool(event: str) -> None:
            tool_calls_log.append({"raw": event})
            if self._on_tool_event:
                self._on_tool_event(event)

        def _on_thinking(text: str) -> None:
            thinking_parts.append(text)

        content = await self._engine.astream_reply(
            user_input=message,
            image_paths=images,
            on_token=lambda _: None,
            on_tool_event=_on_tool,
            on_thinking=_on_thinking,
            on_retry=on_retry,
            system_context=system_context,
            should_stop=should_stop,
        )
        interrupted = should_stop() if should_stop else False
        return ChatResult(
            content=content,
            tool_calls=tool_calls_log,
            thinking="".join(thinking_parts),
            interrupted=interrupted,
        )

    async def astream(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Async-stream tokens as they arrive.

        Usage::

            async for token in agent.astream("hello"):
                print(token, end="", flush=True)

        Tool-call events and thinking deltas are delivered via
        ``on_tool_event`` (constructor arg) and can be inspected post-hoc
        via :attr:`history`.  To capture them live, use
        :meth:`astream_events` instead.
        """
        tool_calls_log: list[dict[str, Any]] = []
        thinking_parts: list[str] = []
        q: asyncio.Queue[str | object] = asyncio.Queue()
        sentinel: object = object()

        async def _on_token(tok: str) -> None:
            await q.put(tok)

        def _on_tool(event: str) -> None:
            tool_calls_log.append({"raw": event})
            if self._on_tool_event:
                self._on_tool_event(event)

        def _on_thinking(text: str) -> None:
            thinking_parts.append(text)

        async def _run() -> None:
            try:
                await self._engine.astream_reply(
                    user_input=message,
                    image_paths=images,
                    on_token=_on_token,
                    on_tool_event=_on_tool,
                    on_thinking=_on_thinking,
                    on_retry=on_retry,
                    system_context=system_context,
                    should_stop=should_stop,
                )
            finally:
                await q.put(sentinel)

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await q.get()
                if item is sentinel:
                    break
                yield item  # type: ignore[misc]
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                # Surface any exception from the worker
                exc = task.exception()
                if exc is not None:
                    raise exc

    async def astream_events(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Async-stream structured events.

        Yields dicts of the form::

            {"type": "token",    "data": "..."}
            {"type": "thinking", "data": "..."}
            {"type": "tool",     "data": "name({args})"}
            {"type": "done",     "data": ChatResult}

        Use this when you need live access to thinking/tool events, not just
        content tokens.
        """
        tool_calls_log: list[dict[str, Any]] = []
        thinking_parts: list[str] = []
        tokens: list[str] = []
        q: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        sentinel: object = object()

        async def _on_token(tok: str) -> None:
            tokens.append(tok)
            await q.put({"type": "token", "data": tok})

        async def _on_tool(event: str) -> None:
            tool_calls_log.append({"raw": event})
            if self._on_tool_event:
                self._on_tool_event(event)
            await q.put({"type": "tool", "data": event})

        async def _on_thinking(text: str) -> None:
            thinking_parts.append(text)
            await q.put({"type": "thinking", "data": text})

        async def _on_retry(event: RetryEvent) -> None:
            await q.put({"type": "retry", "data": event})

        result_box: list[str] = []

        async def _run() -> None:
            try:
                r = await self._engine.astream_reply(
                    user_input=message,
                    image_paths=images,
                    on_token=_on_token,
                    on_tool_event=_on_tool,
                    on_thinking=_on_thinking,
                    on_retry=_on_retry,
                    system_context=system_context,
                    should_stop=should_stop,
                )
                result_box.append(r)
            finally:
                await q.put(sentinel)

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await q.get()
                if item is sentinel:
                    break
                yield item  # type: ignore[misc]
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                exc = task.exception()
                if exc is not None:
                    raise exc

        content = result_box[0] if result_box else "".join(tokens)
        interrupted = bool(should_stop and should_stop())
        yield {
            "type": "done",
            "data": ChatResult(
                content=content,
                tool_calls=tool_calls_log,
                thinking="".join(thinking_parts),
                interrupted=interrupted,
            ),
        }

    async def acompact(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
    ) -> str:
        """Async version of :meth:`compact`."""
        return await self._engine.acompact(
            prompt=prompt, on_token=on_token, on_retry=on_retry
        )

    # ========================================================================
    # History management
    # ========================================================================

    def history(
        self,
        include_system: bool = False,
        include_tool_messages: bool = False,
    ) -> list[dict[str, Any]]:
        """Return conversation messages with optional filtering."""
        msgs = self._engine.export_messages()
        if not include_system:
            msgs = [m for m in msgs if m.get("role") != "system"]
        if not include_tool_messages:
            msgs = [m for m in msgs if m.get("role") != "tool"]
        return msgs

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Raw message list (read-only copy)."""
        return self._engine.export_messages()

    def last_reply(self) -> str | None:
        """Return the content of the last assistant message, or *None*."""
        for msg in reversed(self._engine.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                return content if isinstance(content, str) else json.dumps(content)
        return None

    def reset(self) -> None:
        """Clear conversation history, keeping the system prompt."""
        self._engine.reset_messages()

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace conversation history with *messages*."""
        self._engine.load_messages(messages)

    def revert(self, steps: int = 1) -> int:
        """Revert the last *steps* user turns. Returns count actually reverted."""
        reverted, _, _ = self._engine.revert_user_turns(steps)
        return reverted

    def fork_at_turn(self, user_turn_index: int) -> list[dict[str, Any]]:
        """Return messages up to the Nth user turn (1-based), for forking."""
        return self._engine.fork_at_turn(user_turn_index)

    def user_turn_count(self) -> int:
        """Count user turns in current conversation."""
        return sum(1 for m in self._engine.messages if m.get("role") == "user")

    # ========================================================================
    # MCP management
    # ========================================================================

    @property
    def mcp(self) -> MCPManager:
        """Direct access to the MCP manager."""
        return self._mcp

    def mcp_start(self, name: str) -> tuple[bool, str]:
        """Start a specific MCP server by name."""
        return self._mcp.start(name)

    def mcp_stop(self, name: str) -> tuple[bool, str]:
        """Stop a specific MCP server by name."""
        return self._mcp.stop(name)

    def mcp_enable(self, name: str, save: bool = False) -> bool:
        """Enable an MCP server in config and start it."""
        ok = set_mcp_enabled(self._config, name, True)
        if not ok:
            return False
        self._mcp.start(name)
        if save:
            save_config(self._config, self._config_path)
        return True

    def mcp_disable(self, name: str, save: bool = False) -> bool:
        """Disable an MCP server in config and stop it."""
        ok = set_mcp_enabled(self._config, name, False)
        if not ok:
            return False
        self._mcp.stop(name)
        if save:
            save_config(self._config, self._config_path)
        return True

    def mcp_status(self) -> dict[str, str]:
        """Return status dict: server_name -> RUNNING|STOPPED."""
        return self._mcp.status()

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Return configured MCP servers from config."""
        return list_mcp_servers(self._config)

    # ========================================================================
    # Session management (optional – requires explicit init)
    # ========================================================================

    def init_session_store(
        self,
        base_dir: str | Path = "data/sessions",
        export_dir: str | Path = "data/exports",
    ) -> SessionStore:
        """Initialize session persistence. Returns the SessionStore."""
        self._session_store = SessionStore(
            base_dir=Path(base_dir),
            export_dir=Path(export_dir),
        )
        return self._session_store

    @property
    def session_store(self) -> SessionStore | None:
        return self._session_store

    def save_session(self, name: str | None = None) -> SessionRecord | None:
        """Save current conversation to a session file."""
        if not self._session_store:
            return None
        if not self._session_store.current_session_id:
            self._session_store.begin_new(
                name=name,
                initial_messages=self.messages,
                persisted=True,
            )
        return self._session_store.save_current_messages(self.messages, force=True)

    def load_session(self, session_id: str) -> SessionRecord:
        """Load a session and restore its messages into the engine."""
        if not self._session_store:
            raise RuntimeError(
                "Session store not initialized. Call init_session_store() first."
            )
        record = self._session_store.load(session_id)
        self._session_store.switch_to(record)
        self._engine.load_messages(record.messages)
        return record

    def list_sessions(self) -> list[SessionRecord]:
        """List all persisted sessions."""
        if not self._session_store:
            return []
        return self._session_store.list_sessions()

    # ========================================================================
    # Lifecycle
    # ========================================================================

    def shutdown(self) -> list[str]:
        """Stop all MCP servers and release resources."""
        ex = getattr(self, "_bg_exec", None)
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
            self._bg_exec = None
        return self._mcp.stop_all()

    def __enter__(self) -> Enkiball:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def __repr__(self) -> str:
        return (
            f"Enkiball(provider={self.provider!r}, model={self.model!r}, "
            f"messages={len(self._engine.messages)}, "
            f"tokens={self._engine.token_usage.total_tokens})"
        )
