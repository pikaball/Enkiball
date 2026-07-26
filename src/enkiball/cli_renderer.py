from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


MAX_PREVIEW_LINES = 24
MAX_PREVIEW_CHARS = 12_000
MAX_COMMAND_CHARS = 2_000


def parse_tool_event(event: str) -> tuple[str, dict[str, Any]]:
    name, separator, payload = event.partition("(")
    if not separator or not payload.endswith(")"):
        return event, {}
    try:
        args = json.loads(payload[:-1])
    except json.JSONDecodeError:
        return name, {}
    return name, args if isinstance(args, dict) else {}


def _display_path(path: object, cwd: Path) -> str:
    raw = Path(str(path)).expanduser()
    target = raw if raw.is_absolute() else cwd / raw
    target = target.resolve(strict=False)
    try:
        return str(target.relative_to(cwd)) or "."
    except ValueError:
        return str(target)


def _lexer_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".css": "css",
        ".go": "go",
        ".html": "html",
        ".java": "java",
        ".js": "javascript",
        ".json": "json",
        ".jsx": "jsx",
        ".md": "markdown",
        ".py": "python",
        ".rs": "rust",
        ".sh": "bash",
        ".toml": "toml",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".xml": "xml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(suffix, "text")


def _truncate_preview(content: str) -> tuple[str, str | None]:
    truncated_chars = max(0, len(content) - MAX_PREVIEW_CHARS)
    content = content[:MAX_PREVIEW_CHARS]
    lines = content.splitlines()
    truncated_lines = max(0, len(lines) - MAX_PREVIEW_LINES)
    preview = content if not truncated_lines else "\n".join(lines[:MAX_PREVIEW_LINES])
    details: list[str] = []
    if truncated_lines:
        details.append(f"{truncated_lines} more lines")
    if truncated_chars:
        details.append(f"{truncated_chars} more characters")
    return preview, ", ".join(details) if details else None


def _tool_header(label: str, path: str | None = None) -> Text:
    header = Text()
    header.append("  ")
    header.append(label, style="bold cyan")
    if path:
        header.append("  ")
        header.append(path, style="bright_white")
    return header


def render_tool_event(event: str, cwd: Path) -> RenderableType:
    name, args = parse_tool_event(event)
    path = _display_path(args.get("path", ""), cwd) if args.get("path") else None

    if name == "write_file":
        content = str(args.get("content", ""))
        preview, hidden = _truncate_preview(content)
        body: list[RenderableType] = [
            _tool_header("Write", path),
            Syntax(
                preview,
                _lexer_for_path(path or ""),
                theme="ansi_dark",
                line_numbers=True,
                word_wrap=True,
                background_color="default",
            ),
        ]
        if hidden:
            body.append(Text(f"  ... {hidden}", style="dim"))
        return Group(*body)

    if name == "edit":
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        old_text = old_text[:MAX_PREVIEW_CHARS]
        new_text = new_text[:MAX_PREVIEW_CHARS]
        diff = "\n".join(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{path or 'file'}",
                tofile=f"b/{path or 'file'}",
                n=2,
                lineterm="",
            )
        )
        preview, hidden = _truncate_preview(diff)
        body = [
            _tool_header("Edit all" if args.get("replace_all") else "Edit", path),
            Syntax(
                preview or "(no textual diff)",
                "diff",
                theme="ansi_dark",
                word_wrap=True,
                background_color="default",
            ),
        ]
        if hidden:
            body.append(Text(f"  ... {hidden}", style="dim"))
        return Group(*body)

    if name == "run_command":
        command = str(args.get("command", ""))
        if len(command) > MAX_COMMAND_CHARS:
            command = command[:MAX_COMMAND_CHARS] + "\n# ... command truncated"
        timeout = args.get("timeout_sec")
        label = "Run"
        if timeout is not None:
            label += f"  timeout {timeout}s"
        return Group(
            _tool_header(label),
            Syntax(
                command,
                "bash",
                theme="ansi_dark",
                word_wrap=True,
                background_color="default",
            ),
        )

    if name == "read_file":
        return _tool_header("Read", path)

    summary = Text("  ")
    summary.append(name, style="bold cyan")
    if args:
        safe_args = {key: value for key, value in args.items() if key != "content"}
        payload = json.dumps(safe_args, ensure_ascii=False, default=str)
        if len(payload) > MAX_COMMAND_CHARS:
            payload = payload[:MAX_COMMAND_CHARS] + "..."
        summary.append("  ")
        summary.append(payload, style="dim")
    return summary


class CliRenderer:
    def __init__(self, console: Console, cwd: Path) -> None:
        self.console = console
        self.cwd = cwd
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._live: Live | None = None
        self._mode: str | None = None

    def start(self) -> None:
        self.console.print(Text("assistant", style="bold green"))

    def token(self, token: str) -> None:
        if self._mode == "thinking":
            self._finish_live()
            self._thinking_parts.clear()
        self._mode = "content"
        self._content_parts.append(token)
        self._update_live(Markdown("".join(self._content_parts)))

    def thinking(self, token: str) -> None:
        if self._mode == "content":
            self._finish_live()
            self._content_parts.clear()
        self._mode = "thinking"
        self._thinking_parts.append(token)
        text = Text("".join(self._thinking_parts), style="dim magenta")
        self._update_live(Panel(text, title="thinking", border_style="dim magenta"))

    def tool(self, event: str) -> None:
        self._finish_live()
        self._content_parts.clear()
        self._thinking_parts.clear()
        self._mode = None
        self.console.print(render_tool_event(event, self.cwd))

    def retry(self, event: object) -> None:
        self._finish_live()
        self._content_parts.clear()
        self._thinking_parts.clear()
        self._mode = None
        attempt = getattr(event, "attempt", "?")
        max_retries = getattr(event, "max_retries", "?")
        delay = getattr(event, "delay_seconds", "?")
        error = getattr(event, "error", "")
        message = Text()
        message.append(f"retry {attempt}/{max_retries}", style="bold yellow")
        message.append(f" in {delay}s")
        if error:
            message.append(f"  {error}", style="dim")
        self.console.print(message)

    def pause(self) -> None:
        self._finish_live()

    def finish(self) -> None:
        self._finish_live()

    def _update_live(self, renderable: RenderableType) -> None:
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=False,
                vertical_overflow="visible",
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=False)

    def _finish_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
