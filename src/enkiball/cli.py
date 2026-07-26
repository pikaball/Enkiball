from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import threading
import tty
from pathlib import Path
from typing import Callable

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


def _build_completer() -> WordCompleter:
    return WordCompleter(SLASH_COMPLETIONS, ignore_case=True, sentence=True)


def _build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-j")
    def _insert_newline(event: object) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()

    return kb


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
    ) -> None:
        self.console = console or Console()
        if agent is None:
            try:
                agent = Enkiball(config_path=DEFAULT_CONFIG_PATH, approval=True)
            except FileNotFoundError as e:
                self.console.print(str(e), style="bold red")
                raise
        self.agent = agent
        self.session_store = agent.session_store
        self.handler = CommandHandler(
            self.console,
            agent._config,
            agent._mcp,
            self.session_store,
            config_path=agent._config_path,
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

    def register_tool(self, *args, **kwargs) -> Callable[..., object]:
        """Register a custom tool on the underlying agent."""
        return self.agent.register_tool(*args, **kwargs)

    # -- Main loop ----------------------------------------------------------

    def run(self) -> None:
        agent = self.agent
        console = self.console
        handler = self.handler
        session_store = self.session_store

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self.history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=_build_completer(),
            complete_while_typing=True,
            multiline=True,
            key_bindings=_build_key_bindings(),
        )

        console.print(
            self.banner or "Enkiball CLI ready. Use /help for commands.",
            style="green",
        )
        console.print(
            f"Config: {agent._config_path}  Provider: {agent.provider}  Model: {agent.model}",
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
                keep_running = handler.handle(text, agent._engine, session)
                after_session_id = session_store.current_session_id
                if (
                    self.on_session_change
                    and after_session_id
                    and after_session_id != before_session_id
                ):
                    self.on_session_change(after_session_id)
                if handler.pending_input is not None:
                    prefill_text = handler.pending_input
                if not keep_running:
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

            thinking_active = agent._engine.show_thinking
            renderer = CliRenderer(console, agent.cwd)
            renderer.start()

            interrupted = False
            exit_requested = False
            try:
                with _StreamInputMonitor() as monitor:
                    def _approve_directory(path: Path, operation: str) -> bool:
                        renderer.pause()
                        monitor.pause()
                        try:
                            answer = console.input(
                                f"\n[bold yellow]{operation} requests access outside cwd:[/] "
                                f"{path}\nAllow for this session? [y/N] "
                            )
                            return answer.strip().lower() in {"y", "yes"}
                        finally:
                            monitor.resume()

                    agent._set_approval_handler(_approve_directory)
                    if not session_store.current_session_persisted:
                        session_store.rename_current(
                            _derive_session_title(text, images_for_turn)
                        )
                    if self.turn_lock is None:
                        agent._engine.stream_reply(
                            llm_text,
                            images_for_turn,
                            renderer.token,
                            renderer.tool,
                            on_thinking=renderer.thinking if thinking_active else None,
                            on_retry=renderer.retry,
                            system_context=system_context,
                            should_stop=monitor.should_stop,
                        )
                    else:
                        with self.turn_lock:
                            agent._engine.stream_reply(
                                llm_text,
                                images_for_turn,
                                renderer.token,
                                renderer.tool,
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
        "--no-approval",
        action="store_true",
        help="silently deny tool access outside allowed directories",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        agent = Enkiball(
            config_path=args.config,
            cwd=args.cwd,
            dir_permissions=args.dir_permission,
            approval=not args.no_approval,
        )
        cli = EnkiballCLI(agent)
    except FileNotFoundError:
        raise SystemExit(1) from None
    cli.run()


if __name__ == "__main__":
    main()
