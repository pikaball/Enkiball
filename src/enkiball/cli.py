from __future__ import annotations

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

from .commands import CommandHandler, SLASH_COMPLETIONS
from .config import DEFAULT_CONFIG_PATH, ensure_config, load_config
from .llm import ChatEngine
from .mcp import MCPManager
from .session_store import SessionStore


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
        if len(compact) <= 48:
            return compact
        return f"{compact[:45]}..."
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
        if not sys.stdin.isatty():
            return self
        self._fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=0.2)
        if self._fd is not None and self._old_termios is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)

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
                continue
            if ch == b"\x03":
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


def main() -> None:
    console = Console()
    config_path = ensure_config(DEFAULT_CONFIG_PATH)
    config = load_config(config_path)
    mcp_manager = MCPManager(config)
    engine = ChatEngine(config, mcp_manager)
    session_store = SessionStore()
    handler = CommandHandler(console, config, mcp_manager, session_store, config_path=config_path)

    history_path = Path(".enkiball_history")
    session = PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=_build_completer(),
        complete_while_typing=True,
        multiline=True,
        key_bindings=_build_key_bindings(),
    )

    console.print("Enkiball CLI ready. Use /help for commands.", style="green")
    console.print(f"Config: {config_path}", style="cyan")
    current = session_store.begin_new(initial_messages=engine.export_messages(), persisted=False)
    console.print(f"Session: {current.session_id} (unsaved)", style="cyan")
    for msg in mcp_manager.start_enabled():
        console.print(f"[mcp] {msg}", style="cyan")

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
            keep_running = handler.handle(text, engine, session)
            if handler.pending_input is not None:
                prefill_text = handler.pending_input
            if not keep_running:
                break
            continue

        if not text and not handler.staged_images:
            continue

        images_for_turn = list(handler.staged_images)

        console.print("assistant> ", style="bold green", end="")
        def _on_token(token: str) -> None:
            console.print(token, end="")
            console.file.flush()

        interrupted = False
        exit_requested = False
        try:
            with _StreamInputMonitor() as monitor:
                if not session_store.current_session_persisted:
                    session_store.rename_current(_derive_session_title(text, images_for_turn))
                engine.stream_reply(
                    text,
                    images_for_turn,
                    _on_token,
                    lambda event: console.print(f"\n[tool] {event}", style="cyan"),
                    should_stop=monitor.should_stop,
                )
                interrupted = monitor.interrupted
                exit_requested = monitor.exit_requested

            handler.staged_images.clear()
            if interrupted:
                console.print("\n[interrupted]", style="bold yellow")
            session_store.save_current_messages(engine.export_messages())
            console.print("")
            if exit_requested:
                break
        except KeyboardInterrupt:
            console.print("\n[interrupted]", style="bold yellow")
            handler.staged_images.clear()
            session_store.save_current_messages(engine.export_messages())
            break
        except Exception as e:  # noqa: BLE001
            console.print("")
            console.print(f"LLM error: {e}", style="bold red")
            session_store.save_current_messages(engine.export_messages())

    session_store.save_current_messages(engine.export_messages())
    for msg in mcp_manager.stop_all():
        console.print(f"[mcp] {msg}", style="cyan")


if __name__ == "__main__":
    main()
