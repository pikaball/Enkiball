from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.markdown import Heading, Markdown, TableElement
from rich.padding import Padding
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich import box
from rich.segment import Segment


MAX_PREVIEW_LINES = 24
MAX_PREVIEW_CHARS = 12_000
MAX_COMMAND_CHARS = 2_000


class FlatHeading(Heading):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        text = self.text
        text.justify = "left"
        text.style = "bold bright_white" if self.tag == "h1" else "bold"
        yield text


class CompactTable(TableElement):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.SIMPLE,
            border_style="dim",
            header_style="bold",
            padding=(0, 1),
            collapse_padding=True,
            expand=False,
            show_edge=False,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                table.add_column(
                    column.content,
                    justify=column.justify,
                    overflow="fold",
                )
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*(element.content for element in row.cells))
        yield table


class TerminalMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "heading_open": FlatHeading,
        "table_open": CompactTable,
    }


class ToolBody:
    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        body_options = options.update(width=max(1, options.max_width - 3))
        lines = console.render_lines(self.renderable, body_options, pad=False)
        for line in lines:
            yield Segment("│ ", console.get_style("dim cyan"))
            yield from line
            yield Segment.line()


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
    header.append("└─ ", style="dim cyan")
    header.append(label, style="bold cyan")
    if path:
        header.append("  ")
        header.append(path, style="bright_white")
    return header


def _tool_body(renderable: RenderableType) -> ToolBody:
    return ToolBody(renderable)


def render_tool_event(event: str, cwd: Path) -> RenderableType:
    name, args = parse_tool_event(event)
    path = _display_path(args.get("path", ""), cwd) if args.get("path") else None

    if name == "write_file":
        content = str(args.get("content", ""))
        preview, hidden = _truncate_preview(content)
        body: list[RenderableType] = [
            _tool_header("Write", path),
            _tool_body(
                Syntax(
                    preview,
                    _lexer_for_path(path or ""),
                    theme="ansi_dark",
                    line_numbers=True,
                    word_wrap=True,
                    background_color="default",
                )
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
            _tool_body(
                Syntax(
                    preview or "(no textual diff)",
                    "diff",
                    theme="ansi_dark",
                    word_wrap=True,
                    background_color="default",
                )
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
        label = "Run privileged" if args.get("privilleged") else "Run"
        if timeout is not None:
            label += f"  timeout {timeout}s"
        return Group(
            _tool_header(label),
            _tool_body(
                Syntax(
                    command,
                    "bash",
                    theme="ansi_dark",
                    word_wrap=True,
                    background_color="default",
                )
            ),
        )

    if name == "read_file":
        return _tool_header("Read", path)

    summary = Text("└─ ", style="dim cyan")
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
        self._pending_content = ""
        self._thinking_parts: list[str] = []
        self._live: Live | None = None
        self._mode: str | None = None
        self._response_started = False

    def _start_response(self) -> None:
        if self._response_started:
            return
        marker = Text("● ", style="green")
        marker.append("assistant", style="bold green")
        self.console.print(marker)
        self._response_started = True

    def token(self, token: str) -> None:
        if self._mode == "thinking":
            self._flush_thinking()
        self._start_response()
        self._mode = "content"
        self._pending_content += token
        self._commit_completed_blocks()

    def thinking(self, token: str) -> None:
        if self._mode == "content":
            self._flush_content()
        self._mode = "thinking"
        self._thinking_parts.append(token)
        text = self._thinking_renderable("".join(self._thinking_parts))
        self._update_live(text)

    def tool(self, event: str) -> None:
        self._flush_current()
        self.console.print()
        self._mode = None
        self._response_started = False
        self.console.print(render_tool_event(event, self.cwd))

    def retry(self, event: object) -> None:
        self._flush_current()
        self.console.print()
        self._mode = None
        self._response_started = False
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
        self._flush_current()
        self.console.print()

    def approval(self, path: Path, operation: str) -> None:
        self._stop_live()
        self.console.print()
        privileged = operation == "privileged command"
        title = Text("! permission required", style="bold yellow")
        detail = Text()
        if privileged:
            detail.append("run command outside the sandbox", style="yellow")
        else:
            detail.append(f"{operation} outside workspace\n", style="yellow")
            detail.append(str(path), style="bright_white")
        choices = Text()
        if privileged:
            choices.append("1. allow", style="bold")
            choices.append("    run this command on the host\n", style="dim")
            choices.append("2. deny", style="bold")
            choices.append("     reject the request\n", style="dim")
        else:
            choices.append("1. once", style="bold")
            choices.append("     allow this request only\n", style="dim")
            choices.append("2. always", style="bold")
            choices.append("   allow this directory for the session\n", style="dim")
            choices.append("3. deny", style="bold")
            choices.append("     reject the request\n", style="dim")
        choices.append("Esc", style="bold yellow")
        choices.append("         interrupt this turn", style="dim")
        self.console.print(Group(title, Padding(detail, (0, 0, 0, 2)), choices))

    def history(
        self,
        messages: list[dict[str, Any]],
        title: str,
        show_thinking: bool = False,
    ) -> None:
        heading = Text("session  ", style="dim")
        heading.append(title, style="bold bright_white")
        self.console.print()
        self.console.print(heading)
        for message in messages:
            role = message.get("role")
            if role == "user":
                self._history_user(message.get("content", ""))
            elif role == "assistant":
                self._history_assistant(message, show_thinking)
        self.console.print(Text("end session", style="dim"))
        self.console.print()

    def _history_user(self, content: object) -> None:
        text, images = self._history_user_content(content)
        marker = Text("● ", style="blue")
        marker.append("you", style="bold blue")
        self.console.print(marker)
        if text:
            self.console.print(TerminalMarkdown(text))
        for path in images:
            image = Text("  image  ", style="dim blue")
            image.append(_display_path(path, self.cwd), style="dim")
            self.console.print(image)

    def _history_assistant(
        self, message: dict[str, Any], show_thinking: bool
    ) -> None:
        reasoning = message.get("reasoning_content")
        if show_thinking and isinstance(reasoning, str) and reasoning:
            preview, hidden = _truncate_preview(reasoning)
            thinking = Text("◇ thinking  ", style="dim magenta")
            thinking.append(preview, style="dim")
            if hidden:
                thinking.append(f"  ... {hidden}", style="dim")
            self.console.print(thinking)

        content = message.get("content")
        if isinstance(content, str) and content:
            marker = Text("● ", style="green")
            marker.append("assistant", style="bold green")
            self.console.print(marker)
            self.console.print(TerminalMarkdown(content))

        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name", "tool"))
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, default=str)
            self.console.print(render_tool_event(f"{name}({arguments})", self.cwd))

    @staticmethod
    def _history_user_content(content: object) -> tuple[str, list[str]]:
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content), []
        texts: list[str] = []
        images: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text", "")))
            elif item.get("type") == "input_image" and item.get("path"):
                images.append(str(item["path"]))
        return "".join(texts), images

    def finish(self) -> None:
        self._flush_current()

    def _flush_current(self) -> None:
        if self._mode == "content":
            self._flush_content()
        elif self._mode == "thinking":
            self._flush_thinking()

    def _flush_content(self) -> None:
        self._stop_live()
        if self._pending_content:
            self.console.print(TerminalMarkdown(self._pending_content.rstrip()))
            self._pending_content = ""

    def _flush_thinking(self) -> None:
        self._stop_live()
        if self._thinking_parts:
            self.console.print(self._thinking_renderable("".join(self._thinking_parts)))
            self._thinking_parts.clear()

    def _commit_completed_blocks(self) -> None:
        boundary = self._completed_markdown_boundary(self._pending_content)
        if boundary <= 0:
            return
        committed = self._pending_content[:boundary].rstrip()
        self._pending_content = self._pending_content[boundary:]
        self._stop_live()
        if committed:
            self.console.print(TerminalMarkdown(committed))

    @staticmethod
    def _completed_markdown_boundary(content: str) -> int:
        offset = 0
        boundary = 0
        fence: str | None = None
        for line in content.splitlines(keepends=True):
            stripped = line.lstrip()
            marker = re.match(r"(`{3,}|~{3,})", stripped)
            if marker:
                candidate = marker.group(1)
                if fence is None:
                    fence = candidate[0]
                elif candidate[0] == fence:
                    fence = None
            offset += len(line)
            if fence is None and not line.strip() and line.endswith(("\n", "\r")):
                boundary = offset
        return boundary

    @staticmethod
    def _thinking_renderable(content: str) -> Text:
        preview, hidden = _truncate_preview(content)
        text = Text("◇ thinking  ", style="dim magenta")
        text.append(preview, style="dim")
        if hidden:
            text.append(f"  ... {hidden}", style="dim")
        return text

    def _update_live(self, renderable: RenderableType, refresh: bool = False) -> None:
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="crop",
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=refresh)

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
