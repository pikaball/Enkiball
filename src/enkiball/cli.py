from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import threading
import tty
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from .agent import Enkiball
from .cli_renderer import CliRenderer
from .commands import CommandHandler, SLASH_COMPLETIONS
from .config import DEFAULT_CONFIG_PATH
from .sandbox import TurnInterrupted


def _build_completer(words: list[str] | None = None) -> WordCompleter:
    return WordCompleter(words or SLASH_COMPLETIONS, ignore_case=True, sentence=True)


def _build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-j")
    def _insert_newline(event: object) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()

    return kb


def _build_approval_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _interrupt(event: object) -> None:
        event.app.exit(result="interrupt")

    return kb


def _prompt_approval(
    session: PromptSession[str] | None = None,
    *,
    privileged: bool = False,
) -> str:
    approval_session = session or PromptSession(
        multiline=False,
        key_bindings=_build_approval_key_bindings(),
        complete_while_typing=False,
        reserve_space_for_menu=0,
    )
    try:
        choices = "1/2" if privileged else "1/2/3"
        default = "2" if privileged else "3"
        answer = approval_session.prompt(
            f"Select [{choices}, Esc=interrupt, default {default}]: "
        )
    except EOFError:
        return "deny"
    if answer == "interrupt":
        raise TurnInterrupted
    if privileged:
        return "once" if answer.strip().lower() in ("1", "once", "allow") else "deny"
    return {
        "1": "once",
        "once": "once",
        "2": "always",
        "always": "always",
    }.get(answer.strip().lower(), "deny")


def _derive_session_title(text: str, image_paths: list[str]) -> str:
    compact = " ".join(text.split())
    if compact:
        return compact[:48] if len(compact) <= 48 else f"{compact[:45]}..."
    if image_paths:
        return f"images-{len(image_paths)}"
    return "untitled"


class _StreamInputMonitor:
    def __init__(self) -> None:
        self._fd: int | None = None
        self._old_termios: list[object] | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stop_event = threading.Event()
        self._exit_event = threading.Event()

    def __enter__(self) -> _StreamInputMonitor:
        self.resume()
        return self

    def pause(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=0.2)
            self._thread = None
        if self._fd is not None and self._old_termios is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)

    def resume(self) -> None:
        if not sys.stdin.isatty():
            return
        self._fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.pause()

    def _loop(self) -> None:
        assert self._fd is not None
        while self._running.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except Exception:
                return
            if not ready:
                continue
            try:
                ch = os.read(self._fd, 1)
            except OSError:
                return
            if ch == b"\x1b":
                self._stop_event.set()
            elif ch == b"\x03":
                self._stop_event.set()
                self._exit_event.set()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    @property
    def interrupted(self) -> bool:
        return self._stop_event.is_set()

    @property
    def exit_requested(self) -> bool:
        return self._exit_event.is_set()


class EnkiballCLI:
    """Reusable CLI runner.

    Lets external code pre-configure an :class:`Enkiball` agent (tools,
    system prompt, etc.), register custom slash commands, then drop into
    the interactive REPL.

    Example::

        from enkiball import Enkiball, EnkiballCLI

        agent = Enkiball()
        agent.register_tool(my_func, name="scan", description="Scan host",
                            parameters={...})

        cli = EnkiballCLI(agent)

        def _cmd_status(argv, handler, engine, session):
            handler.console.print(f"tokens: {engine.token_usage}")
            return True

        cli.register_command("status", _cmd_status, "Show token usage")
        cli.run()
    """

    def __init__(
        self,
        agent: Enkiball | None = None,
        *,
        console: Console | None = None,
        history_path: Path | None = None,
        banner: str | None = None,
        input_transform: Callable[[str, list[str]], str | tuple[str, str | None]] | None = None,
        turn_lock: object | None = None,
        on_session_change: Callable[[str], None] | None = None,
        debug_scope: dict[str, Any] | None = None,
        btw_context: Callable[[], str] | None = None,
    ) -> None:
        self.console = console or Console()
        if agent is None:
            try:
                agent = Enkiball(config_path=DEFAULT_CONFIG_PATH, approval=True)
            except FileNotFoundError as e:
                self.console.print(str(e), style="bold red")
                raise
        self.agent = agent
        self.debug_scope = debug_scope if debug_scope is not None else {}
        self.debug_scope.setdefault("agent", self.agent)
        self.debug_scope.setdefault("cli", self)
        self.session_store = agent.session_store
        self.handler = CommandHandler(
            self.console,
            agent,
            debug_scope=self.debug_scope,
            btw_context=btw_context,
        )
        self.history_path = history_path or (
            Path.home() / ".config" / "enkiball" / "history"
        )
        self.banner = banner
        self.input_transform = input_transform
        self.turn_lock = turn_lock
        self.on_session_change = on_session_change

    # -- Extensibility shortcuts --------------------------------------------

    def register_command(
        self,
        name: str,
        handler: Callable[..., bool],
        help_text: str = "",
        completions: list[str] | None = None,
    ) -> None:
        """Register a custom slash command (delegates to ``CommandHandler``)."""
        self.handler.register_command(name, handler, help_text, completions)

    # -- Main loop ----------------------------------------------------------

    def run(self) -> None:
        agent = self.agent
        console = self.console
        handler = self.handler
        session_store = self.session_store

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self.history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=_build_completer(handler.completions),
            complete_while_typing=True,
            multiline=True,
            key_bindings=_build_key_bindings(),
        )

        console.print(
            self.banner or "Enkiball CLI ready. Use /help for commands.",
            style="green",
        )
        console.print(
            f"Config: {agent.config_path}  Provider: {agent.provider}  Model: {agent.model}"
            f"  Thinking: {'on' if agent.show_thinking else 'off'}",
            style="cyan",
        )
        current = session_store.begin_new(
            initial_messages=agent.messages, persisted=False
        )
        if self.on_session_change:
            self.on_session_change(current.session_id)
        console.print(f"Session: {current.session_id} (unsaved)", style="cyan")

        prefill_text: str | None = None
        while True:
            prompt_kwargs: dict[str, str] = {}
            if prefill_text is not None:
                prompt_kwargs["default"] = prefill_text
            try:
                line = session.prompt("you> ", **prompt_kwargs)
            except KeyboardInterrupt:
                break
            except EOFError:
                break
            prefill_text = None

            text = line.strip()

            if text.startswith("/"):
                before_session_id = session_store.current_session_id
                command_exit_requested = False
                # Slash commands may perform a synchronous model turn (for
                # example /btw), so give them the same Esc cancellation path
                # as an ordinary foreground turn.
                with _StreamInputMonitor() as monitor:
                    handler.set_stop_callback(monitor.should_stop)
                    try:
                        keep_running = handler.handle(text, session)
                    except TurnInterrupted:
                        console.print("\n[interrupted]", style="bold yellow")
                        keep_running = True
                    finally:
                        handler.set_stop_callback(None)
                    command_exit_requested = monitor.exit_requested
                after_session_id = session_store.current_session_id
                if (
                    self.on_session_change
                    and after_session_id
                    and after_session_id != before_session_id
                ):
                    self.on_session_change(after_session_id)
                if (
                    handler.pending_history_replay
                    and after_session_id
                    and session_store.has_user_turn(agent.messages)
                ):
                    CliRenderer(console, agent.cwd).history(
                        agent.messages,
                        title=session_store.current_session_name or after_session_id,
                        show_thinking=agent.show_thinking,
                    )
                handler.pending_history_replay = False
                if handler.pending_input is not None:
                    prefill_text = handler.pending_input
                if command_exit_requested or not keep_running:
                    break
                continue

            if not text and not handler.staged_images:
                continue

            images_for_turn = list(handler.staged_images)
            llm_text = text
            system_context: str | None = None
            if self.input_transform:
                transformed = self.input_transform(text, images_for_turn)
                if isinstance(transformed, tuple):
                    llm_text, system_context = transformed
                else:
                    llm_text = transformed

            thinking_active = agent.show_thinking
            renderer = CliRenderer(console, agent.cwd)

            interrupted = False
            exit_requested = False
            try:
                with _StreamInputMonitor() as monitor:
                    def _approve_directory(path: Path, operation: str) -> str:
                        privileged = operation == "privileged command"
                        renderer.pause()
                        monitor.pause()
                        try:
                            renderer.approval(path, operation)
                            return _prompt_approval(privileged=privileged)
                        finally:
                            monitor.resume()

                    agent._set_approval_handler(_approve_directory)
                    if not session_store.current_session_persisted:
                        session_store.rename_current(
                            _derive_session_title(text, images_for_turn)
                        )
                    if self.turn_lock is None:
                        agent.chat(
                            llm_text,
                            images_for_turn,
                            on_token=renderer.token,
                            on_tool_event=renderer.tool,
                            on_thinking=renderer.thinking if thinking_active else None,
                            on_retry=renderer.retry,
                            system_context=system_context,
                            should_stop=monitor.should_stop,
                        )
                    else:
                        with self.turn_lock:
                            agent.chat(
                                llm_text,
                                images_for_turn,
                                on_token=renderer.token,
                                on_tool_event=renderer.tool,
                                on_thinking=renderer.thinking if thinking_active else None,
                                on_retry=renderer.retry,
                                system_context=system_context,
                                should_stop=monitor.should_stop,
                            )
                    interrupted = monitor.interrupted
                    exit_requested = monitor.exit_requested

                handler.staged_images.clear()
                if interrupted:
                    console.print("\n[interrupted]", style="bold yellow")
                session_store.save_current_messages(agent.messages)
                console.print("")
                if exit_requested:
                    break
            except KeyboardInterrupt:
                console.print("\n[interrupted]", style="bold yellow")
                handler.staged_images.clear()
                session_store.save_current_messages(agent.messages)
                break
            except TurnInterrupted:
                console.print("\n[interrupted]", style="bold yellow")
                handler.staged_images.clear()
                session_store.save_current_messages(agent.messages)
            except Exception as e:  # noqa: BLE001
                console.print("")
                console.print(f"LLM error: {e}", style="bold red")
                session_store.save_current_messages(agent.messages)
            finally:
                renderer.finish()
                agent._set_approval_handler(None)

        session_store.save_current_messages(agent.messages)
        agent.shutdown()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enkiball interactive CLI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dir-permission",
        action="append",
        default=[],
        metavar="DIR",
        help="allow tool access to an additional directory; repeatable",
    )
    parser.add_argument(
        "--ro-permission",
        action="append",
        default=[],
        metavar="DIR",
        help="allow read-only tool access to an additional directory; repeatable",
    )
    parser.add_argument(
        "--no-approval",
        action="store_true",
        help="silently deny tool access outside allowed directories",
    )
    parser.add_argument(
        "--sandbox-network",
        choices=("none", "host"),
        default="none",
        help="network access for sandboxed commands (default: none)",
    )
    parser.add_argument(
        "--sandbox-env",
        action="append",
        default=[],
        metavar="NAME",
        help="inherit a host environment variable in the sandbox; repeatable",
    )
    parser.add_argument(
        "--privilleged",
        action="store_true",
        help="disable command sandboxing and allow file access to any directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        agent = Enkiball(
            config_path=args.config,
            cwd=args.cwd,
            dir_permissions=[Path(args.cwd).resolve().anchor]
            if args.privilleged
            else args.dir_permission,
            approval=not args.no_approval,
            sandbox=not args.privilleged,
            sandbox_network=args.sandbox_network,
            sandbox_inherit_env=args.sandbox_env,
            ro_permissions=args.ro_permission,
        )
        cli = EnkiballCLI(agent)
    except FileNotFoundError:
        raise SystemExit(1) from None
    cli.run()


if __name__ == "__main__":
    main()
