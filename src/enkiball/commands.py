from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from rich.console import Console

from .config import (
    DEFAULT_CONFIG_PATH,
    get_active_model,
    get_active_provider,
    get_provider_models,
    get_providers,
    list_mcp_servers,
    save_config,
    set_active_model,
    set_active_provider,
    set_mcp_enabled,
)
from .mcp import MCPManager
from .session_store import SessionStore

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

    from .llm import ChatEngine

MCP_ACTIONS = ("list", "enable", "disable", "start", "stop", "status", "path", "help")
SESSION_ACTIONS = ("list", "current", "new", "use", "rename", "export", "fork", "help")
IMAGE_ACTIONS = ("add", "list", "remove", "clear", "help")
SLASH_COMPLETIONS = [
    "/help",
    "/exit",
    "/model",
    "/provider",
    "/provider list",
    "/provider switch",
    "/compact",
    "/autocompact",
    "/autocompact on",
    "/autocompact off",
    "/revert",
    "/thinking",
    "/tokens",
    "/image",
    "/image add",
    "/image list",
    "/image remove",
    "/image clear",
    "/image help",
    "/mcp",
    "/mcp list",
    "/mcp enable",
    "/mcp disable",
    "/mcp start",
    "/mcp stop",
    "/mcp status",
    "/mcp path",
    "/mcp help",
    "/session",
    "/session list",
    "/session current",
    "/session new",
    "/session use",
    "/session rename",
    "/session export",
    "/session fork",
    "/session help",
]

HELP_TEXT = """\
Slash commands:
  /help                      Show this help
  /exit                      Exit CLI
  /model [name]              Show or switch model
  /provider                  Show current provider
  /provider list             List all providers
  /provider switch <name>    Switch provider
  /compact                   Summarize history to reduce context sent to LLM
  /autocompact [on|off]      Show or toggle automatic compaction
  /revert [n]                Revert last n user turns and edit/resend
  /thinking                  Toggle thinking/reasoning output
  /tokens                    Show accumulated token usage
  /image add <path...>       Stage one or more images for next user message
  /image list                Show staged images
  /image remove <index>      Remove one staged image
  /image clear               Clear all staged images
  /session                   Enter interactive session panel
  /session list              List sessions
  /session current           Show current session
  /session new [name]        Create and switch to a new unsaved session
  /session use <id|index>    Switch session (restores dialogue on screen)
  /session rename <name>     Rename current session
  /session export [path]     Export current full dialogue to markdown
  /session fork <turn>       Fork a new session from user turn N
  /mcp                       Enter interactive MCP panel
  /mcp list                  List MCP servers
  /mcp enable <name|index>   Enable and start MCP server
  /mcp disable <name|index>  Disable and stop MCP server
  /mcp start <name|index>    Start MCP server process
  /mcp stop <name|index>     Stop MCP server process
  /mcp status                Show MCP process status
  /mcp path                  Print MCP config path
"""

IMAGE_HELP = """\
Image commands:
  add <path...>      Stage one or more local image files
  list               Show staged images
  remove <index>     Remove one staged image
  clear              Clear staged images
  help               Show this help
"""

MCP_HELP = """\
MCP panel commands:
  list
  enable <name|index>
  disable <name|index>
  start <name|index>
  stop <name|index>
  status
  path
  help
  back
"""

SESSION_HELP = """\
Session panel commands:
  list
  current
  new [name]
  use <id|index>
  rename <name>
  export [path]
  fork <turn>        Fork from user turn N
  help
  back
"""


class CommandHandler:
    def __init__(
        self,
        console: Console,
        config: dict[str, Any],
        mcp_manager: MCPManager,
        session_store: SessionStore,
        config_path: Path = DEFAULT_CONFIG_PATH,
    ):
        self.console = console
        self.config = config
        self.mcp_manager = mcp_manager
        self.session_store = session_store
        self.config_path = config_path
        self.pending_input: str | None = None
        self.last_raw_line: str = ""
        self.staged_images: list[str] = []
        # Custom command registry: "/foo" -> (handler, help_text)
        #   handler signature: (argv, self, engine, session) -> bool (True = keep running)
        self._custom_commands: dict[
            str,
            tuple[
                Callable[[list[str], "CommandHandler", "ChatEngine", "PromptSession[str]"], bool],
                str,
            ],
        ] = {}

    # -- Extensibility -------------------------------------------------------

    def register_command(
        self,
        name: str,
        handler: Callable[
            [list[str], "CommandHandler", "ChatEngine", "PromptSession[str]"], bool
        ],
        help_text: str = "",
        completions: list[str] | None = None,
    ) -> None:
        """Register a custom slash command.

        Parameters
        ----------
        name : str
            Command name with or without leading slash, e.g. ``"foo"`` or ``"/foo"``.
        handler : callable
            ``(argv, handler, engine, session) -> bool``.  Return True to keep
            the CLI running, False to exit.  ``argv[0]`` is the command itself.
        help_text : str
            Shown in /help.
        completions : list[str] | None
            Extra completions to add (e.g. subcommand suggestions).
        """
        key = name if name.startswith("/") else f"/{name}"
        self._custom_commands[key] = (handler, help_text)
        if key not in SLASH_COMPLETIONS:
            SLASH_COMPLETIONS.append(key)
        if completions:
            for c in completions:
                full = c if c.startswith("/") else f"{key} {c}"
                if full not in SLASH_COMPLETIONS:
                    SLASH_COMPLETIONS.append(full)

    def unregister_command(self, name: str) -> bool:
        key = name if name.startswith("/") else f"/{name}"
        existed = key in self._custom_commands
        self._custom_commands.pop(key, None)
        return existed

    def list_custom_commands(self) -> dict[str, str]:
        return {k: h[1] for k, h in self._custom_commands.items()}

    def handle(
        self, raw_line: str, engine: ChatEngine, session: PromptSession[str]
    ) -> bool:
        self.pending_input = None
        self.last_raw_line = raw_line
        try:
            argv = shlex.split(raw_line)
        except ValueError as e:
            self.console.print(f"command parse error: {e}", style="bold red")
            return True

        if not argv:
            return True

        cmd = argv[0]
        if cmd == "/help":
            self.console.print(HELP_TEXT)
            if self._custom_commands:
                self.console.print("Custom commands:", style="bold")
                for k, (_, ht) in self._custom_commands.items():
                    self.console.print(f"  {k:26} {ht}")
            return True
        if cmd == "/exit":
            return False
        if cmd == "/model":
            return self._model(argv, engine)
        if cmd == "/provider":
            return self._provider(argv)
        if cmd == "/compact":
            return self._compact(engine)
        if cmd == "/autocompact":
            return self._autocompact(argv, engine)
        if cmd == "/revert":
            return self._revert(argv, engine)
        if cmd == "/thinking":
            return self._thinking(engine)
        if cmd == "/tokens":
            return self._tokens(engine)
        if cmd == "/image":
            return self._image(argv)
        if cmd == "/session":
            return self._session(argv, session, engine)
        if cmd == "/mcp":
            return self._mcp(argv, session)

        # Custom user-registered commands
        if cmd in self._custom_commands:
            fn, _ = self._custom_commands[cmd]
            try:
                return bool(fn(argv, self, engine, session))
            except Exception as e:  # noqa: BLE001
                self.console.print(
                    f"error in custom command {cmd}: {e}", style="bold red"
                )
                return True

        self.console.print(f"unknown command: {cmd}", style="bold yellow")
        return True

    # -- /model --------------------------------------------------------------

    def _model(self, argv: list[str], engine: ChatEngine) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"provider: {get_active_provider(self.config)}  model: {engine.model}",
                style="cyan",
            )
            # Show available models (config + remote)
            self.console.print("fetching models...", style="dim")
            models = get_provider_models(self.config, remote=True, timeout=5.0)
            if models:
                self.console.print(f"available: {', '.join(models)}", style="dim")
            else:
                self.console.print("no models found", style="yellow")
            return True
        new_model = argv[1]
        set_active_model(self.config, new_model)
        self.console.print(f"model switched to: {engine.model}", style="green")
        return True

    # -- /provider -----------------------------------------------------------

    def _provider(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"active provider: {get_active_provider(self.config)}",
                style="cyan",
            )
            return True
        sub = argv[1]
        if sub == "list":
            for name, prov in get_providers(self.config).items():
                active = "*" if name == get_active_provider(self.config) else " "
                models = get_provider_models(
                    self.config, provider_name=name, remote=True, timeout=3.0
                )
                models_str = ", ".join(models) if models else "(none)"
                base = prov.get("base_url", "")
                self.console.print(
                    f"  {active} {name:16} models=[{models_str}]  base_url={base}"
                )
            return True
        if sub == "switch":
            if len(argv) < 3:
                self.console.print(
                    "usage: /provider switch <name>", style="bold yellow"
                )
                return True
            target = argv[2]
            if set_active_provider(self.config, target):
                self.console.print(
                    f"switched to {target}, model={get_active_model(self.config)}",
                    style="green",
                )
            else:
                self.console.print(f"unknown provider: {target}", style="bold red")
            return True
        self.console.print("usage: /provider [list|switch <name>]", style="bold yellow")
        return True

    # -- /compact ------------------------------------------------------------

    def _compact(self, engine: ChatEngine) -> bool:
        msg_count = len(engine.messages)
        if msg_count <= 2:
            self.console.print("not enough history to compact", style="yellow")
            return True
        self.console.print("compacting conversation...", style="cyan")
        summary = engine.compact(
            on_token=lambda tok: self.console.print(tok, end="", highlight=False),
        )
        self.console.print()  # newline after streamed summary
        before = msg_count
        after = len(engine.messages) - engine.compact_index
        self.console.print(
            f"compact done. {before} messages in history, "
            f"{after} messages will be sent to LLM.",
            style="green",
        )
        self._save_current_session(engine)
        return True

    # -- /autocompact --------------------------------------------------------

    def _autocompact(self, argv: list[str], engine: ChatEngine) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"auto_compact={engine.auto_compact_enabled} "
                f"threshold_ratio={engine.auto_compact_threshold_ratio} "
                f"effective_threshold_tokens={engine._auto_compact_effective_threshold_tokens()}",
                style="cyan",
            )
            return True
        if len(argv) == 2 and argv[1] in {"on", "off"}:
            engine.auto_compact_enabled = argv[1] == "on"
            state = "enabled" if engine.auto_compact_enabled else "disabled"
            self.console.print(f"auto_compact {state}", style="green")
            return True
        self.console.print("usage: /autocompact [on|off]", style="bold yellow")
        return True

    # -- /revert -------------------------------------------------------------

    def _revert(self, argv: list[str], engine: ChatEngine) -> bool:
        if len(argv) > 2:
            self.console.print("usage: /revert [n]", style="bold yellow")
            return True
        steps = 1
        if len(argv) == 2:
            if not argv[1].isdigit() or int(argv[1]) <= 0:
                self.console.print(
                    "usage: /revert [n], n must be positive integer",
                    style="bold yellow",
                )
                return True
            steps = int(argv[1])

        reverted, reverted_input, reverted_images = engine.revert_user_turns(steps)
        if reverted == 0:
            self.console.print("no user turn to revert", style="yellow")
            return True
        self.pending_input = reverted_input or ""
        self.staged_images = list(reverted_images)
        self._save_current_session(engine)

        # Visual feedback: clear reverted output
        self.console.print(f"--- reverted {reverted} turn(s) ---", style="bold yellow")
        self.console.print("edit input and press Enter to resend.", style="green")
        if self.staged_images:
            self.console.print(
                f"restored {len(self.staged_images)} image(s) into staging.",
                style="cyan",
            )
        return True

    # -- /thinking -----------------------------------------------------------

    def _thinking(self, engine: ChatEngine) -> bool:
        engine.show_thinking = not engine.show_thinking
        state = "ON" if engine.show_thinking else "OFF"
        self.console.print(f"thinking output: {state}", style="cyan")
        return True

    # -- /tokens -------------------------------------------------------------

    def _tokens(self, engine: ChatEngine) -> bool:
        u = engine.token_usage
        self.console.print(
            f"prompt={u.prompt_tokens}  completion={u.completion_tokens}  total={u.total_tokens}",
            style="cyan",
        )
        return True

    # -- /image --------------------------------------------------------------

    def _image_add(self, argv: list[str]) -> None:
        if len(argv) < 3:
            self.console.print("usage: /image add <path...>", style="bold yellow")
            return
        added = 0
        for raw in argv[2:]:
            fp = Path(raw).expanduser().resolve()
            if not fp.exists():
                self.console.print(f"image not found: {fp}", style="bold red")
                continue
            if not fp.is_file():
                self.console.print(f"not a file: {fp}", style="bold red")
                continue
            value = str(fp)
            if value in self.staged_images:
                continue
            self.staged_images.append(value)
            added += 1
        self.console.print(
            f"staged images: {len(self.staged_images)} total ({added} added)",
            style="green",
        )

    def _image_list(self) -> None:
        if not self.staged_images:
            self.console.print("no staged images", style="yellow")
            return
        for i, path in enumerate(self.staged_images, start=1):
            self.console.print(f"{i:2d}. {path}")

    def _image_remove(self, argv: list[str]) -> None:
        if len(argv) != 3:
            self.console.print("usage: /image remove <index>", style="bold yellow")
            return
        raw = argv[2]
        if not raw.isdigit():
            self.console.print("index must be a positive integer", style="bold yellow")
            return
        idx = int(raw) - 1
        if idx < 0 or idx >= len(self.staged_images):
            self.console.print(f"invalid index: {raw}", style="bold red")
            return
        removed = self.staged_images.pop(idx)
        self.console.print(f"removed image: {removed}", style="green")

    def _image(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self._image_list()
            self.console.print("tip: /image add <path...>", style="cyan")
            return True
        action = argv[1]
        if action == "add":
            self._image_add(argv)
            return True
        if action == "list":
            self._image_list()
            return True
        if action == "remove":
            self._image_remove(argv)
            return True
        if action == "clear":
            self.staged_images.clear()
            self.console.print("staged images cleared", style="green")
            return True
        if action == "help":
            self.console.print(IMAGE_HELP)
            return True
        # Shortcut: `/image <path...>` equals add.
        self._image_add(["/image", "add", *argv[1:]])
        return True

    # -- /mcp ----------------------------------------------------------------

    def _resolve_mcp_target(self, raw: str) -> str | None:
        servers = list_mcp_servers(self.config)
        if raw.isdigit():
            idx = int(raw) - 1
            names = list(servers.keys())
            if 0 <= idx < len(names):
                return names[idx]
            return None
        if raw in servers:
            return raw
        return None

    def _print_mcp_list(self) -> None:
        servers = list_mcp_servers(self.config)
        if not servers:
            self.console.print("no MCP servers in config", style="yellow")
            return
        status_map = self.mcp_manager.status()
        for i, (name, item) in enumerate(servers.items(), start=1):
            enabled = "ON " if item.get("enabled") else "OFF"
            run = status_map.get(name, "STOPPED")
            self.console.print(f"{i:2d}. {name:16} enabled={enabled} runtime={run}")

    def _set_mcp_enabled(self, target: str, enabled: bool) -> None:
        ok = set_mcp_enabled(self.config, target, enabled)
        if not ok:
            self.console.print(f"unknown MCP server: {target}", style="bold red")
            return
        save_config(self.config, self.config_path)
        if enabled:
            started, msg = self.mcp_manager.start(target)
            self.console.print(f"MCP {target} enabled=True", style="green")
            self.console.print(msg, style=("green" if started else "bold red"))
        else:
            self.mcp_manager.stop(target)
            self.console.print(f"MCP {target} enabled=False, stopped", style="green")

    def _mcp_action(self, action: str, raw_target: str | None) -> None:
        if action in {"enable", "disable", "start", "stop"} and not raw_target:
            self.console.print(f"usage: {action} <name|index>", style="bold yellow")
            return

        if action in {"enable", "disable", "start", "stop"}:
            target = self._resolve_mcp_target(raw_target or "")
            if not target:
                self.console.print(
                    f"unknown MCP server: {raw_target}", style="bold red"
                )
                return
            if action == "enable":
                self._set_mcp_enabled(target, True)
                return
            if action == "disable":
                self._set_mcp_enabled(target, False)
                return
            if action == "start":
                ok, msg = self.mcp_manager.start(target)
                self.console.print(msg, style=("green" if ok else "bold red"))
                return
            if action == "stop":
                ok, msg = self.mcp_manager.stop(target)
                self.console.print(msg, style=("green" if ok else "bold red"))
                return

        if action == "status":
            for name, status in self.mcp_manager.status().items():
                self.console.print(f"{name:16} {status}")
            return
        if action == "path":
            self.console.print(str(self.config_path), style="cyan")
            return
        if action == "list":
            self._print_mcp_list()
            return
        if action == "help":
            self.console.print(MCP_HELP)
            return

        self.console.print("unknown mcp action", style="bold yellow")

    def _mcp_interactive(self, session: PromptSession[str]) -> bool:
        self.console.print(
            "Enter MCP panel. Type `help` for commands, `back` to leave.", style="cyan"
        )
        self._print_mcp_list()
        while True:
            try:
                line = session.prompt("mcp> ")
            except KeyboardInterrupt:
                return True  # exit sub-panel, stay in main CLI
            except EOFError:
                return True
            text = line.strip()
            if not text:
                continue
            if text in {"back", "exit", "quit"}:
                return True
            try:
                argv = shlex.split(text)
            except ValueError as e:
                self.console.print(f"command parse error: {e}", style="bold red")
                continue
            action = argv[0]
            target = argv[1] if len(argv) >= 2 else None
            self._mcp_action(action, target)

    def _mcp(self, argv: list[str], session: PromptSession[str]) -> bool:
        if len(argv) == 1:
            return self._mcp_interactive(session)
        sub = argv[1]
        if sub in {"list", "status", "path", "help"}:
            self._mcp_action(sub, None)
            return True
        if sub in {"enable", "disable", "start", "stop"}:
            target = argv[2] if len(argv) >= 3 else None
            self._mcp_action(sub, target)
            return True
        self.console.print(
            f"usage: /mcp [{'|'.join(MCP_ACTIONS)}]",
            style="bold yellow",
        )
        return True

    # -- /session ------------------------------------------------------------

    def _print_session_list(self) -> None:
        sessions = self.session_store.list_sessions()
        current = self.session_store.current_session_id
        if current and not self.session_store.current_session_persisted:
            name = self.session_store.current_session_name or "untitled"
            self.console.print(f"* {current:30} name={name} (unsaved)")
        if not sessions:
            if not current or self.session_store.current_session_persisted:
                self.console.print("no sessions", style="yellow")
            return
        for idx, item in enumerate(sessions, start=1):
            mark = "*" if item.session_id == current else " "
            self.console.print(
                f"{idx:2d}. {mark} {item.session_id:30} name={item.name} updated={item.updated_at}"
            )

    def _save_current_session(self, engine: ChatEngine) -> None:
        self.session_store.save_current_messages(engine.export_messages())

    def _replay_session(self, engine: ChatEngine) -> None:
        """Print the loaded session's conversation history on screen."""
        for msg in engine.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                continue
            if role == "tool":
                continue
            if not isinstance(content, str):
                import json as _json

                content = _json.dumps(content, ensure_ascii=False)
            if role == "user":
                self.console.print(f"you> {content}", style="dim")
            elif role == "assistant":
                tc = msg.get("tool_calls")
                if tc:
                    names = ", ".join(
                        c.get("function", {}).get("name", "?")
                        for c in tc
                        if isinstance(c, dict)
                    )
                    self.console.print(f"assistant> [tool: {names}]", style="dim cyan")
                if content:
                    self.console.print(f"assistant> {content}", style="dim green")

    def _session_action(self, action: str, args: list[str], engine: ChatEngine) -> None:
        if action == "list":
            self._print_session_list()
            return
        if action == "current":
            sid = self.session_store.current_session_id
            if not sid:
                self.console.print("no current session", style="yellow")
                return
            name = self.session_store.current_session_name or sid
            saved = (
                "saved" if self.session_store.current_session_persisted else "unsaved"
            )
            self.console.print(f"{sid} ({name}, {saved})", style="cyan")
            return
        if action == "help":
            self.console.print(SESSION_HELP)
            return
        if action == "new":
            name = " ".join(args).strip() if args else None
            self._save_current_session(engine)
            engine.reset_messages()
            created = self.session_store.begin_new(
                name=name,
                initial_messages=engine.export_messages(),
                persisted=False,
            )
            self.staged_images.clear()
            self.console.print(
                f"switched to new session: {created.session_id} (unsaved)",
                style="green",
            )
            return
        if action == "use":
            if not args:
                self.console.print("usage: use <id|index>", style="bold yellow")
                return
            resolved = self.session_store.resolve(args[0])
            if not resolved:
                self.console.print(f"unknown session: {args[0]}", style="bold red")
                return
            self._save_current_session(engine)
            record = self.session_store.load(resolved)
            self.session_store.switch_to(record)
            engine.load_messages(record.messages)
            self.staged_images.clear()
            self.console.print(f"switched session: {record.session_id}", style="green")
            self.console.print("--- session history ---", style="dim")
            self._replay_session(engine)
            self.console.print("--- end of history ---", style="dim")
            return
        if action == "rename":
            sid = self.session_store.current_session_id
            if not sid:
                self.console.print("no current session", style="yellow")
                return
            new_name = " ".join(args).strip()
            if not new_name:
                self.console.print("usage: rename <name>", style="bold yellow")
                return
            if not self.session_store.rename_current(new_name):
                self.console.print("failed to rename current session", style="bold red")
                return
            saved = (
                "saved" if self.session_store.current_session_persisted else "unsaved"
            )
            self.console.print(f"session renamed: {new_name} ({saved})", style="green")
            return
        if action == "export":
            sid = self.session_store.current_session_id
            if not sid:
                self.console.print("no current session", style="yellow")
                return
            if not SessionStore.has_user_turn(engine.export_messages()):
                self.console.print(
                    "current session has no dialogue yet", style="yellow"
                )
                return
            self._save_current_session(engine)
            out = self.session_store.export_session(
                sid, out_path=args[0] if args else None
            )
            self.console.print(f"exported: {out}", style="green")
            return
        if action == "fork":
            if not args or not args[0].isdigit():
                self.console.print("usage: fork <turn_number>", style="bold yellow")
                return
            turn = int(args[0])
            forked_msgs = engine.fork_at_turn(turn)
            if not forked_msgs or not SessionStore.has_user_turn(forked_msgs):
                self.console.print(f"no user turn {turn} found", style="bold red")
                return
            self._save_current_session(engine)
            engine.load_messages(forked_msgs)
            fork_name = f"fork-turn{turn}"
            created = self.session_store.begin_new(
                name=fork_name,
                initial_messages=engine.export_messages(),
                persisted=True,
            )
            self.staged_images.clear()
            self.console.print(
                f"forked to new session: {created.session_id}", style="green"
            )
            return

        self.console.print("unknown session action", style="bold yellow")

    def _session_interactive(
        self, session: PromptSession[str], engine: ChatEngine
    ) -> bool:
        self.console.print(
            "Enter session panel. Type `help` for commands, `back` to leave.",
            style="cyan",
        )
        self._print_session_list()
        while True:
            try:
                line = session.prompt("session> ")
            except KeyboardInterrupt:
                return True  # exit sub-panel, stay in main CLI
            except EOFError:
                return True
            text = line.strip()
            if not text:
                continue
            if text in {"back", "exit", "quit"}:
                return True
            try:
                argv = shlex.split(text)
            except ValueError as e:
                self.console.print(f"command parse error: {e}", style="bold red")
                continue
            action = argv[0]
            self._session_action(action, argv[1:], engine)

    def _session(
        self, argv: list[str], session: PromptSession[str], engine: ChatEngine
    ) -> bool:
        if len(argv) == 1:
            return self._session_interactive(session, engine)
        action = argv[1]
        if action not in SESSION_ACTIONS:
            self.console.print(
                f"usage: /session [{'|'.join(SESSION_ACTIONS)}]", style="bold yellow"
            )
            return True
        self._session_action(action, argv[2:], engine)
        return True
