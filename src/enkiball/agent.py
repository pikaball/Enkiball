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
        agent.mcp.start("ssh-mcp")
        agent.mcp.stop("ssh-mcp")
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
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Literal,
    Mapping,
)

from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_STATE_PATH,
    ensure_config,
    get_active_model,
    get_active_provider,
    get_provider_models,
    get_providers,
    load_config,
    load_state,
    save_config,
    set_active_model,
    set_active_provider,
    set_mcp_enabled,
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
from .sandbox import ApprovalHandler, Sandbox
from .session_store import SessionRecord, SessionStore
from .skills import DEFAULT_SKILL_DIR, Skill, SkillRegistry


@dataclass
class ChatResult:
    """Structured result from a single chat turn."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str = ""
    interrupted: bool = False

    def __str__(self) -> str:
        return self.content


class _ChatResultCollector:
    def __init__(
        self,
        on_tool_event: Callable[[str], None] | None = None,
        on_thinking_event: Callable[[str], None] | None = None,
    ) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.thinking_parts: list[str] = []
        self.on_tool_event = on_tool_event
        self.on_thinking_event = on_thinking_event

    def on_tool(self, event: str) -> None:
        self.tool_calls.append({"raw": event})
        if self.on_tool_event:
            self.on_tool_event(event)

    def on_thinking(self, text: str) -> None:
        self.thinking_parts.append(text)
        if self.on_thinking_event:
            self.on_thinking_event(text)

    def result(self, content: str, interrupted: bool = False) -> ChatResult:
        return ChatResult(
            content=content,
            tool_calls=self.tool_calls,
            thinking="".join(self.thinking_parts),
            interrupted=interrupted,
        )


class _NoticeQueue(list[str]):
    """List-compatible queue with atomic snapshot-and-drain support."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()

    def append(self, item: str) -> None:
        with self._lock:
            super().append(item)

    def extend(self, items: Iterable[str]) -> None:
        with self._lock:
            super().extend(items)

    def insert(self, index: int, item: str) -> None:
        with self._lock:
            super().insert(index, item)

    def clear(self) -> None:
        with self._lock:
            super().clear()

    def pop(self, index: int = -1) -> str:
        with self._lock:
            return super().pop(index)

    def remove(self, item: str) -> None:
        with self._lock:
            super().remove(item)

    def snapshot(self) -> list[str]:
        with self._lock:
            return super().copy()

    def drain(self) -> list[str]:
        with self._lock:
            notices = super().copy()
            super().clear()
            return notices


class Enkiball:
    """High-level programmatic interface to the Enkiball agent.

    Parameters
    ----------
    config_path : str | Path
        Path to ``agent.json``.  Defaults to ``config/agent.json``.
    state_path : str | Path
        Path for active provider/model state. Defaults to the shared Enkiball
        state file. Loaded once at startup; persisted switches do not affect
        other running instances.
    system_prompt : str | None
        Override the default system prompt.
    auto_start_mcp : bool
        If *True* (default), enabled MCP servers are started on init.
    on_tool_event : callable | None
        Optional callback ``(event_str) -> None`` fired before each tool
        execution.  Useful for logging / auditing.
    session_dir : str | Path | None
        Directory for persisted session JSON files. This is the only public
        way to customize session storage location.
    cwd : str | Path | None
        Working directory available to built-in file and command tools.
    dir_permissions : list[str | Path] | None
        Additional writable directories available to built-in tools.
    ro_permissions : list[str | Path] | None
        Additional read-only directories available to built-in tools.
    approval : bool
        Allow an interactive frontend to request access outside configured
        directories. Without a frontend, access is denied.
    sandbox : bool
        Use the command sandbox. When false, commands run directly in the host
        environment. Defaults to true.
    sandbox_network : {"none", "host"}
        Network access for sandboxed commands. Defaults to ``"none"``.
    sandbox_env : Mapping[str, str] | None
        Environment variables explicitly supplied to sandboxed commands.
    sandbox_inherit_env : Iterable[str] | None
        Names of host environment variables inherited by sandboxed commands.
    skill_dirs : list[str | Path] | None
        Directories containing skill folders. Defaults to
        ``~/.config/enkiball/skills``. Pass an empty list to disable skills.
    """

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        system_prompt: str | None = None,
        auto_start_mcp: bool = True,
        on_tool_event: Callable[[str], None] | None = None,
        session_dir: str | Path | None = None,
        cwd: str | Path | None = None,
        dir_permissions: list[str | Path] | None = None,
        approval: bool = False,
        sandbox: bool = True,
        sandbox_network: Literal["none", "host"] = "none",
        sandbox_env: Mapping[str, str] | None = None,
        sandbox_inherit_env: Iterable[str] | None = None,
        model: str | None = None,
        state_path: str | Path = DEFAULT_STATE_PATH,
        skill_dirs: list[str | Path] | None = None,
        ro_permissions: list[str | Path] | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._state_path = Path(state_path)
        ensure_config(self._config_path)
        state = load_state(self._state_path)
        self._config = load_config(self._config_path, self._state_path, state=state)

        self._sandbox = Sandbox(
            cwd,
            dir_permissions,
            approval,
            sandbox,
            network=sandbox_network,
            env=sandbox_env,
            inherit_env=sandbox_inherit_env,
            ro_permissions=ro_permissions,
        )
        self._skill_registry = SkillRegistry(
            [DEFAULT_SKILL_DIR] if skill_dirs is None else skill_dirs
        )
        self._sandbox.set_managed_read_roots("skills", self._skill_registry.roots)
        self._mcp = MCPManager(self._config)
        self.notice_queue: list[str] = _NoticeQueue()
        self._engine = ChatEngine(
            self._config, self._mcp, self._sandbox, state_path=self._state_path,
            model=model, state=state,
        )
        self._engine.notice_queue = self.notice_queue
        self._on_tool_event = on_tool_event
        self._session_store = self._build_session_store(session_dir, self.cwd)
        self._sync_skill_tool()

        if system_prompt is not None:
            self._engine.set_system_prompt(system_prompt)

        if auto_start_mcp:
            self._mcp.start_enabled()

    @property
    def cwd(self) -> Path:
        return self._sandbox.cwd

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def dir_permissions(self) -> tuple[Path, ...]:
        return self._sandbox.allowed_directories

    @property
    def ro_permissions(self) -> tuple[Path, ...]:
        return self._sandbox.read_only_directories

    def _set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._sandbox.set_approval_handler(handler)

    @staticmethod
    def _build_session_store(
        session_dir: str | Path | None, cwd: Path
    ) -> SessionStore:
        if session_dir is None:
            return SessionStore(cwd=cwd)
        base_dir = Path(session_dir).expanduser().resolve()
        return SessionStore._from_paths(
            base_dir, base_dir.parent / "exports", cwd=cwd
        )

    # ========================================================================
    # Custom tools
    # ========================================================================

    def _sync_skill_tool(self) -> None:
        if self._skill_registry.skills:
            self._engine.set_internal_tool(
                "skill",
                self._skill_registry.tool_schema(),
                self._skill_registry.handle_tool,
            )
        else:
            self._engine.set_internal_tool("skill", None, None)

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._skill_registry.skills

    @property
    def skill_diagnostics(self) -> tuple[str, ...]:
        return self._skill_registry.diagnostics

    def load_skill(self, name: str, resource: str | None = None) -> str:
        return self._skill_registry.load(name, resource)

    def reload_skills(self) -> tuple[Skill, ...]:
        self._skill_registry.reload()
        self._sandbox.set_managed_read_roots("skills", self._skill_registry.roots)
        self._sync_skill_tool()
        return self.skills

    def _with_explicit_skills(
        self, message: str, system_context: str | None
    ) -> str | None:
        registry = getattr(self, "_skill_registry", None)
        if registry is None:
            return system_context
        skill_context = registry.activation_context(message)
        if skill_context is None:
            return system_context
        if system_context:
            return f"{system_context}\n\n{skill_context}"
        return skill_context

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

            self._engine.register_tool(tool_name_eff, full_schema, fn)
            return fn

        if func is None:
            # Used as decorator with kwargs
            return _do_register  # type: ignore[return-value]
        return _do_register(func)

    def unregister_tool(self, name: str) -> bool:
        """Remove a previously registered custom tool.  Returns True if found."""
        return self._engine.unregister_tool(name)

    def list_custom_tools(self) -> list[str]:
        """Return names of registered custom tools."""
        return self._engine.list_custom_tools()

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
        return get_active_provider(self._config, self._state_path, state=self._engine.state)

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all configured providers and their settings."""
        return json.loads(json.dumps(get_providers(self._config)))

    def switch_provider(self, name: str) -> bool:
        """Switch to a different provider.  Returns False if not found.

        The selection is always persisted to ``state.json``.
        """
        switched = set_active_provider(
            self._config, name, self._state_path, state=self._engine.state
        )
        if switched:
            self._engine._model_override = None
        return switched

    def switch_model(self, model: str) -> None:
        """Switch model (within current provider).

        The selection is always persisted to ``state.json``.
        """
        self._engine.model = model

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
            state_path=self._state_path,
            state=self._engine.state,
        )

    @property
    def system_prompt(self) -> str:
        return self._engine.system_prompt

    @system_prompt.setter
    def system_prompt(self, prompt: str) -> None:
        self._engine.set_system_prompt(prompt)

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
        should_stop: Callable[[], bool] | None = None,
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
        return self._engine.compact(
            prompt=prompt, on_token=on_token, on_retry=on_retry, should_stop=should_stop
        )

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
        on_token: Callable[[str], None] | None = None,
        on_tool_event: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResult:
        """Send *message* and block until the full reply is collected."""
        def _emit_tool(event: str) -> None:
            if self._on_tool_event:
                self._on_tool_event(event)
            if on_tool_event and on_tool_event is not self._on_tool_event:
                on_tool_event(event)

        collector = _ChatResultCollector(
            _emit_tool if self._on_tool_event or on_tool_event else None,
            on_thinking,
        )
        system_context = self._with_explicit_skills(message, system_context)

        content = self._engine.stream_reply(
            user_input=message,
            image_paths=images,
            on_token=on_token or (lambda _: None),
            on_tool_event=collector.on_tool,
            on_thinking=collector.on_thinking,
            on_retry=on_retry,
            system_context=system_context,
            should_stop=should_stop,
        )
        return collector.result(content, interrupted=self._engine.interrupted())

    def btw(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_token: Callable[[str], None] | None = None,
        on_tool_event: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        system_context: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResult:
        """Run a one-off question without adding it to this agent's history.

        The temporary turn uses the same model, configured tools, MCP clients,
        sandbox, and conversation context as the foreground agent. Tool side
        effects are intentionally preserved; only conversation messages and
        token accounting are restored after the turn.
        """
        messages = self._engine.export_messages()
        usage = TokenUsage(**self._engine.token_usage.to_dict())
        last_interrupted = self._engine.interrupted()
        try:
            return self.chat(
                message,
                images,
                on_token=on_token,
                on_tool_event=on_tool_event,
                on_thinking=on_thinking,
                on_retry=on_retry,
                system_context=system_context,
                should_stop=should_stop,
            )
        finally:
            self._engine.load_messages(messages)
            self._engine.token_usage = usage
            self._engine._last_turn_interrupted = last_interrupted

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
        system_context = self._with_explicit_skills(message, system_context)
        tokens: list[str] = []
        collector = _ChatResultCollector(self._on_tool_event)
        _q: queue.Queue[str] = queue.Queue()
        _sentinel = object()

        def _on_token(tok: str) -> None:
            _q.put(tok)
            tokens.append(tok)

        result_box: list[str] = []
        exc_box: list[Exception] = []
        abandon_event = threading.Event()

        def _should_stop() -> bool:
            return abandon_event.is_set() or bool(should_stop and should_stop())

        def _worker() -> None:
            try:
                r = self._engine.stream_reply(
                    user_input=message,
                    image_paths=images,
                    on_token=_on_token,
                    on_tool_event=collector.on_tool,
                    on_thinking=collector.on_thinking,
                    on_retry=on_retry,
                    system_context=system_context,
                    should_stop=_should_stop,
                )
                result_box.append(r)
            except Exception as e:
                exc_box.append(e)
            finally:
                _q.put(_sentinel)  # type: ignore[arg-type]

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        completed = False
        try:
            while True:
                item = _q.get()
                if item is _sentinel:
                    completed = True
                    break
                yield item  # type: ignore[misc]
        finally:
            if not completed:
                abandon_event.set()
            t.join()
        if exc_box:
            raise exc_box[0]

        content = result_box[0] if result_box else "".join(tokens)
        return collector.result(content, interrupted=self._engine.interrupted())

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
        collector = _ChatResultCollector(self._on_tool_event)
        system_context = self._with_explicit_skills(message, system_context)

        content = await self._engine.astream_reply(
            user_input=message,
            image_paths=images,
            on_token=lambda _: None,
            on_tool_event=collector.on_tool,
            on_thinking=collector.on_thinking,
            on_retry=on_retry,
            system_context=system_context,
            should_stop=should_stop,
        )
        return collector.result(content, interrupted=self._engine.interrupted())

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
        async for event in self.astream_events(
            message,
            images,
            on_retry=on_retry,
            system_context=system_context,
            should_stop=should_stop,
        ):
            if event["type"] == "token":
                yield event["data"]

    async def astream_events(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
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
        collector = _ChatResultCollector(self._on_tool_event)
        system_context = self._with_explicit_skills(message, system_context)
        tokens: list[str] = []
        q: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        sentinel: object = object()

        async def _on_token(tok: str) -> None:
            tokens.append(tok)
            await q.put({"type": "token", "data": tok})

        async def _on_tool(event: str) -> None:
            collector.on_tool(event)
            await q.put({"type": "tool", "data": event})

        async def _on_thinking(text: str) -> None:
            collector.on_thinking(text)
            await q.put({"type": "thinking", "data": text})

        async def _on_retry(event: RetryEvent) -> None:
            if on_retry is not None:
                callback_result = on_retry(event)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
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
                self._engine.interrupt()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                exc = task.exception()
                if exc is not None:
                    raise exc

        content = result_box[0] if result_box else "".join(tokens)
        interrupted = self._engine.interrupted()
        yield {
            "type": "done",
            "data": collector.result(content, interrupted=interrupted),
        }

    async def acompact(
        self,
        prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
        on_retry: Callable[[RetryEvent], Awaitable[None] | None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """Async version of :meth:`compact`."""
        return await self._engine.acompact(
            prompt=prompt, on_token=on_token, on_retry=on_retry, should_stop=should_stop
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

    def mcp_enable(self, name: str, save: bool = False) -> bool:
        """Enable an MCP server in config and start it."""
        ok = set_mcp_enabled(self._config, name, True)
        if not ok:
            return False
        started, _ = self._mcp.start(name)
        if not started:
            set_mcp_enabled(self._config, name, False)
            return False
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

    # ========================================================================
    # Session management (optional – requires explicit init)
    # ========================================================================

    @property
    def session_store(self) -> SessionStore:
        return self._session_store

    def save_session(self, name: str | None = None) -> SessionRecord | None:
        """Save current conversation to a session file."""
        if not self._session_store.current_session_id:
            self._session_store.begin_new(
                name=name,
                initial_messages=self.messages,
                persisted=True,
            )
        return self._session_store.save_current_messages(self.messages, force=True)

    def load_session(self, session_id: str) -> SessionRecord:
        """Load a session and restore its messages into the engine."""
        record = self._session_store.load(session_id)
        self._session_store.switch_to(record)
        self._engine.load_messages(record.messages)
        return record

    def list_sessions(self) -> list[SessionRecord]:
        """List all persisted sessions."""
        return self._session_store.list_sessions()

    # ========================================================================
    # Lifecycle
    # ========================================================================

    def shutdown(self) -> list[str]:
        """Stop all MCP servers and release resources."""
        self.interrupt()
        ex = getattr(self, "_bg_exec", None)
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)
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
