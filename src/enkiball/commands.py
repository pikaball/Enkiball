from __future__ import annotations

import ast
import inspect
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from rich.console import Console

from .config import list_mcp_servers
from .message_codec import extract_user_content
from .session_store import SessionStore

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

    from .agent import Enkiball


@dataclass(frozen=True)
class CommandContext:
    agent: "Enkiball"
    console: Console
    session_store: SessionStore
    prompt_session: "PromptSession[str]"
    should_stop: Callable[[], bool] | None = None

MCP_ACTIONS = ("list", "enable", "disable", "start", "stop", "status", "path", "help")
SESSION_ACTIONS = ("list", "current", "new", "use", "rename", "export", "fork", "help")
IMAGE_ACTIONS = ("add", "list", "remove", "clear", "help")
NOTICE_ACTIONS = ("show", "add")
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
    "/btw",
    "/debug",
    "/notice",
    "/notice show",
    "/notice add",
    "/image",
    "/image add",
    "/image list",
    "/image remove",
    "/image clear",
    "/image help",
    "/mcp list",
    "/mcp enable",
    "/mcp disable",
    "/mcp start",
    "/mcp stop",
    "/mcp status",
    "/mcp path",
    "/mcp help",
    "/session list",
    "/session current",
    "/session new",
    "/session use",
    "/session rename",
    "/session export",
    "/session fork",
    "/session help",
]
BUILTIN_COMMANDS = {item.split()[0] for item in SLASH_COMPLETIONS}

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
  /btw <prompt...>           Ask a one-off question without changing history
  /debug <python>            Execute Python in the CLI process
  /notice show               Show queued system notices
  /notice add <message>      Queue a notice for the next tool result
  /image add <path...>       Stage one or more images for next user message
  /image list                Show staged images
  /image remove <index>      Remove one staged image
  /image clear               Clear all staged images
  /session list              List sessions
  /session current           Show current session
  /session new [name]        Create and switch to a new unsaved session
  /session use <id|index>    Switch session (restores dialogue on screen)
  /session rename <name>     Rename current session
  /session export [path]     Export current full dialogue to markdown
  /session fork <turn>       Fork a new session from user turn N
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
MCP commands:
  /mcp list
  /mcp enable <name|index>
  /mcp disable <name|index>
  /mcp start <name|index>
  /mcp stop <name|index>
  /mcp status
  /mcp path
  /mcp help
"""

SESSION_HELP = """\
Session commands:
  /session list
  /session current
  /session new [name]
  /session use <id|index>
  /session rename <name>
  /session export [path]
  /session fork <turn>        Fork from user turn N
  /session help
"""


class CommandHandler:
    def __init__(
        self,
        console: Console,
        agent: "Enkiball",
        debug_scope: dict[str, Any] | None = None,
        btw_context: Callable[[], str] | None = None,
    ):
        self.console = console
        self.agent = agent
        self.session_store = agent.session_store
        self.debug_scope = debug_scope if debug_scope is not None else {}
        self.btw_context = btw_context
        self.completions = list(SLASH_COMPLETIONS)
        self.pending_input: str | None = None
        self.pending_history_replay: bool = False
        self.last_raw_line: str = ""
        self._should_stop: Callable[[], bool] | None = None
        self.staged_images: list[str] = []
        self._custom_commands: dict[str, tuple[Callable[..., bool], str]] = {}
        self._command_completions: dict[str, list[str]] = {}

    def set_stop_callback(self, callback: Callable[[], bool] | None) -> None:
        """Set the active CLI cancellation callback for long-running commands."""
        self._should_stop = callback

    def should_stop(self) -> bool:
        return bool(self._should_stop and self._should_stop())

    # -- Extensibility -------------------------------------------------------

    def register_command(
        self,
        name: str,
        handler: Callable[..., bool],
        help_text: str = "",
        completions: list[str] | None = None,
    ) -> None:
        """Register a custom slash command.

        Parameters
        ----------
        name : str
            Command name with or without leading slash, e.g. ``"foo"`` or ``"/foo"``.
        handler : callable
            ``(argv, context) -> bool``. Return True to keep the CLI running,
            False to exit. ``argv[0]`` is the command itself.
        help_text : str
            Shown in /help.
        completions : list[str] | None
            Extra completions to add (e.g. subcommand suggestions).
        """
        key = name if name.startswith("/") else f"/{name}"
        if key in BUILTIN_COMMANDS:
            raise ValueError(f"reserved command name: {key}")
        if key in self._custom_commands:
            raise ValueError(f"command already registered: {key}")
        parameter_count = len(inspect.signature(handler).parameters)
        if parameter_count != 2:
            raise TypeError("command handler must accept (argv, context)")
        self._custom_commands[key] = (handler, help_text)
        added = [key]
        if key not in self.completions:
            self.completions.append(key)
        if completions:
            for c in completions:
                full = c if c.startswith("/") else f"{key} {c}"
                added.append(full)
                if full not in self.completions:
                    self.completions.append(full)
        self._command_completions[key] = added

    def unregister_command(self, name: str) -> bool:
        key = name if name.startswith("/") else f"/{name}"
        existed = key in self._custom_commands
        self._custom_commands.pop(key, None)
        for completion in self._command_completions.pop(key, []):
            if completion not in SLASH_COMPLETIONS:
                try:
                    self.completions.remove(completion)
                except ValueError:
                    pass
        return existed

    def list_custom_commands(self) -> dict[str, str]:
        return {k: value[1] for k, value in self._custom_commands.items()}

    def handle(self, raw_line: str, session: PromptSession[str]) -> bool:
        self.pending_input = None
        self.last_raw_line = raw_line
        if raw_line == "/debug" or (
            raw_line.startswith("/debug")
            and len(raw_line) > len("/debug")
            and raw_line[len("/debug")].isspace()
        ):
            return self._debug(raw_line[len("/debug"):].lstrip())
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
            return self._model(argv)
        if cmd == "/provider":
            return self._provider(argv)
        if cmd == "/compact":
            return self._compact()
        if cmd == "/autocompact":
            return self._autocompact(argv)
        if cmd == "/revert":
            return self._revert(argv)
        if cmd == "/thinking":
            return self._thinking()
        if cmd == "/tokens":
            return self._tokens()
        if cmd == "/btw":
            return self._btw(argv)
        if cmd == "/notice":
            return self._notice(argv)
        if cmd == "/image":
            return self._image(argv)
        if cmd == "/session":
            return self._session(argv)
        if cmd == "/mcp":
            return self._mcp(argv)

        # Custom user-registered commands
        if cmd in self._custom_commands:
            fn, _ = self._custom_commands[cmd]
            try:
                context = CommandContext(
                    agent=self.agent,
                    console=self.console,
                    session_store=self.session_store,
                    prompt_session=session,
                    should_stop=self.should_stop,
                )
                return bool(fn(argv, context))
            except Exception as e:  # noqa: BLE001
                self.console.print(
                    f"error in custom command {cmd}: {e}", style="bold red"
                )
                return True

        self.console.print(f"unknown command: {cmd}", style="bold yellow")
        return True

    def _btw(self, argv: list[str]) -> bool:
        if len(argv) < 2:
            self.console.print("usage: /btw <prompt...>", style="yellow")
            return True
        try:
            result = self.agent.btw(
                " ".join(argv[1:]),
                system_context=self.btw_context() if self.btw_context else None,
                should_stop=self.should_stop,
            )
            if result.content:
                self.console.print(result.content)
            if result.interrupted:
                self.console.print("[interrupted]", style="bold yellow")
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"btw error: {exc}", style="bold red")
        return True

    # -- /debug -------------------------------------------------------------

    def _debug(self, code: str) -> bool:
        if not code:
            self.console.print("usage: /debug <python>", style="yellow")
            self.console.print("locals: " + ", ".join(self.debug_scope), style="dim")
            return True

        try:
            module = ast.parse(code, filename="<enkiball-debug>", mode="exec")
            result: Any = None
            has_result = bool(module.body and isinstance(module.body[-1], ast.Expr))
            if has_result:
                statements = ast.Module(
                    body=module.body[:-1], type_ignores=module.type_ignores
                )
                if statements.body:
                    exec(  # noqa: S102
                        compile(statements, "<enkiball-debug>", "exec"),
                        self.debug_scope,
                        self.debug_scope,
                    )
                expression = ast.Expression(module.body[-1].value)
                result = eval(  # noqa: S307
                    compile(expression, "<enkiball-debug>", "eval"),
                    self.debug_scope,
                    self.debug_scope,
                )
            else:
                exec(  # noqa: S102
                    compile(module, "<enkiball-debug>", "exec"),
                    self.debug_scope,
                    self.debug_scope,
                )
            if has_result:
                self.console.print(repr(result), markup=False)
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"debug error: {exc!r}", style="bold red")
        return True

    # -- /model --------------------------------------------------------------

    def _model(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"provider: {self.agent.provider}  model: {self.agent.model}",
                style="cyan",
            )
            # Show available models (config + remote)
            self.console.print("fetching models...", style="dim")
            models = self.agent.list_models(remote=True, timeout=5.0)
            if models:
                self.console.print(f"available: {', '.join(models)}", style="dim")
            else:
                self.console.print("no models found", style="yellow")
            return True
        new_model = argv[1]
        self.agent.switch_model(new_model)
        self.console.print(f"model switched to: {self.agent.model}", style="green")
        return True

    # -- /provider -----------------------------------------------------------

    def _provider(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"active provider: {self.agent.provider}",
                style="cyan",
            )
            return True
        sub = argv[1]
        if sub == "list":
            for name, prov in self.agent.providers.items():
                active = "*" if name == self.agent.provider else " "
                models = self.agent.list_models(
                    provider_name=name, remote=True, timeout=3.0
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
            if self.agent.switch_provider(target):
                self.console.print(
                    f"switched to {target}, model={self.agent.model}",
                    style="green",
                )
            else:
                self.console.print(f"unknown provider: {target}", style="bold red")
            return True
        self.console.print("usage: /provider [list|switch <name>]", style="bold yellow")
        return True

    # -- /compact ------------------------------------------------------------

    def _compact(self) -> bool:
        msg_count = len(self.agent.messages)
        if msg_count <= 2:
            self.console.print("not enough history to compact", style="yellow")
            return True
        self.console.print("compacting conversation...", style="cyan")
        summary = self.agent.compact(
            on_token=lambda tok: self.console.print(tok, end="", highlight=False),
            should_stop=self.should_stop,
        )
        self.console.print()  # newline after streamed summary
        self.console.print(
            f"compact done. {msg_count} messages retained in history.",
            style="green",
        )
        self._save_current_session()
        return True

    # -- /autocompact --------------------------------------------------------

    def _autocompact(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"auto_compact={self.agent.auto_compact_enabled} "
                f"threshold_ratio={self.agent.auto_compact_threshold_ratio} "
                "effective_threshold_tokens="
                f"{self.agent.auto_compact_effective_threshold_tokens}",
                style="cyan",
            )
            return True
        if len(argv) == 2 and argv[1] in {"on", "off"}:
            self.agent.auto_compact_enabled = argv[1] == "on"
            state = "enabled" if self.agent.auto_compact_enabled else "disabled"
            self.console.print(f"auto_compact {state}", style="green")
            return True
        self.console.print("usage: /autocompact [on|off]", style="bold yellow")
        return True

    # -- /revert -------------------------------------------------------------

    def _revert(self, argv: list[str]) -> bool:
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

        user_messages = [
            message for message in self.agent.messages if message.get("role") == "user"
        ]
        restored_content = (
            user_messages[-min(steps, len(user_messages))].get("content", "")
            if user_messages
            else ""
        )
        reverted_input, reverted_images = extract_user_content(restored_content)
        reverted = self.agent.revert(steps)
        if reverted == 0:
            self.console.print("no user turn to revert", style="yellow")
            return True
        self.pending_input = reverted_input or ""
        self.staged_images = list(reverted_images)
        self._save_current_session()

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

    def _thinking(self) -> bool:
        self.agent.set_show_thinking(not self.agent.show_thinking)
        state = "ON" if self.agent.show_thinking else "OFF"
        self.console.print(f"thinking output: {state}", style="cyan")
        return True

    # -- /tokens -------------------------------------------------------------

    def _tokens(self) -> bool:
        u = self.agent.token_usage
        self.console.print(
            f"prompt={u.prompt_tokens}  completion={u.completion_tokens}  total={u.total_tokens}",
            style="cyan",
        )
        return True

    # -- /notice ------------------------------------------------------------

    def _notice(self, argv: list[str]) -> bool:
        if self.agent is None:
            self.console.print("notice queue unavailable", style="bold red")
            return True
        if len(argv) == 2 and argv[1] == "show":
            notice_queue = self.agent.notice_queue
            snapshot = getattr(notice_queue, "snapshot", None)
            notices = snapshot() if callable(snapshot) else list(notice_queue)
            if not notices:
                self.console.print("no queued notices", style="yellow")
                return True
            for index, notice in enumerate(notices, start=1):
                self.console.print(f"{index:2d}. {notice}", markup=False)
            return True
        if len(argv) >= 3 and argv[1] == "add":
            notice = " ".join(argv[2:])
            self.agent.notice_queue.append(notice)
            self.console.print(
                f"queued notices: {len(self.agent.notice_queue)}", style="green"
            )
            return True
        self.console.print(
            f"usage: /notice [{'|'.join(NOTICE_ACTIONS)}] [message]",
            style="bold yellow",
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
        servers = list_mcp_servers(self.agent.config)
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
        servers = list_mcp_servers(self.agent.config)
        if not servers:
            self.console.print("no MCP servers in config", style="yellow")
            return
        status_map = self.agent.mcp.status()
        for i, (name, item) in enumerate(servers.items(), start=1):
            enabled = "ON " if item.get("enabled") else "OFF"
            run = status_map.get(name, "STOPPED")
            self.console.print(f"{i:2d}. {name:16} enabled={enabled} runtime={run}")

    def _set_mcp_enabled(self, target: str, enabled: bool) -> None:
        if enabled:
            ok = self.agent.mcp_enable(target, save=True)
        else:
            ok = self.agent.mcp_disable(target, save=True)
        if not ok:
            self.console.print(f"failed to set MCP {target} enabled={enabled}", style="bold red")
            return
        self.console.print(f"MCP {target} enabled={enabled}", style="green")

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
                ok, msg = self.agent.mcp.start(target)
                self.console.print(msg, style=("green" if ok else "bold red"))
                return
            if action == "stop":
                ok, msg = self.agent.mcp.stop(target)
                self.console.print(msg, style=("green" if ok else "bold red"))
                return

        if action == "status":
            for name, status in self.agent.mcp.status().items():
                self.console.print(f"{name:16} {status}")
            return
        if action == "path":
            self.console.print(str(self.agent.config_path), style="cyan")
            return
        if action == "list":
            self._print_mcp_list()
            return
        if action == "help":
            self.console.print(MCP_HELP)
            return

        self.console.print("unknown mcp action", style="bold yellow")

    def _mcp(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"usage: /mcp [{'|'.join(MCP_ACTIONS)}]",
                style="bold yellow",
            )
            return True
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

    def _save_current_session(self) -> None:
        self.session_store.save_current_messages(self.agent.messages)

    def _session_action(self, action: str, args: list[str]) -> None:
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
            self._save_current_session()
            self.agent.reset()
            created = self.session_store.begin_new(
                name=name,
                initial_messages=self.agent.messages,
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
            self._save_current_session()
            record = self.session_store.load(resolved)
            self.session_store.switch_to(record)
            self.agent.load_messages(record.messages)
            self.staged_images.clear()
            self.pending_history_replay = True
            self.console.print(f"switched session: {record.session_id}", style="green")
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
            if not SessionStore.has_user_turn(self.agent.messages):
                self.console.print(
                    "current session has no dialogue yet", style="yellow"
                )
                return
            self._save_current_session()
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
            forked_msgs = self.agent.fork_at_turn(turn)
            if not forked_msgs or not SessionStore.has_user_turn(forked_msgs):
                self.console.print(f"no user turn {turn} found", style="bold red")
                return
            self._save_current_session()
            self.agent.load_messages(forked_msgs)
            fork_name = f"fork-turn{turn}"
            created = self.session_store.begin_new(
                name=fork_name,
                initial_messages=self.agent.messages,
                persisted=True,
            )
            self.staged_images.clear()
            self.pending_history_replay = True
            self.console.print(
                f"forked to new session: {created.session_id}", style="green"
            )
            return

        self.console.print("unknown session action", style="bold yellow")

    def _session(self, argv: list[str]) -> bool:
        if len(argv) == 1:
            self.console.print(
                f"usage: /session [{'|'.join(SESSION_ACTIONS)}]",
                style="bold yellow",
            )
            return True
        action = argv[1]
        if action not in SESSION_ACTIONS:
            self.console.print(
                f"usage: /session [{'|'.join(SESSION_ACTIONS)}]", style="bold yellow"
            )
            return True
        self._session_action(action, argv[2:])
        return True
