from __future__ import annotations

import difflib
import json
from copy import copy
from pathlib import Path
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.markdown import BlockQuote, CodeBlock, Heading, ListItem, Markdown, Paragraph, TableElement
from rich.padding import Padding
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich import box
from rich.segment import Segment

from .cli_math import markdown_parser, render_latex


MAX_PREVIEW_LINES = 24
MAX_PREVIEW_CHARS = 12_000
MAX_COMMAND_CHARS = 2_000


def _syntax_text(code: str, lexer: str, *, line_numbers: bool = False) -> Text:
    """Highlight logical lines without Syntax's viewport padding or folding."""
    text = Syntax(
        code, lexer, theme="ansi_dark", background_color="default",
    ).highlight(code)
    if not code.endswith("\n") and text.plain.endswith("\n"):
        text = text[:-1]
    if line_numbers and text:
        lines = text.split("\n", allow_blank=True)
        if text.plain.endswith("\n"):
            lines.pop()
        numbered = Text()
        width = len(str(len(lines)))
        for number, line in enumerate(lines, 1):
            if number > 1:
                numbered.append("\n")
            numbered.append(f"{number:>{width}} ", style="dim")
            numbered.append_text(line)
        if text.plain.endswith("\n"):
            numbered.append("\n")
        text = numbered
    text.no_wrap = True
    text.overflow = "ignore"
    text.end = "" if text.plain.endswith("\n") else "\n"
    return text


def _prefixed_lines(
    renderable: RenderableType,
    console: Console,
    options: ConsoleOptions,
    first: Segment,
    continuation: Segment | None = None,
) -> RenderResult:
    """Prefix source lines; leave visual wrapping to the terminal."""
    continuation = first if continuation is None else continuation
    render_options = options.update(
        width=max(1, options.max_width - max(first.cell_length, continuation.cell_length)),
        no_wrap=True,
        overflow="ignore",
    )
    segments = console.render(renderable, render_options)
    for index, (line, newline) in enumerate(Segment.split_lines_terminator(segments)):
        yield first if index == 0 else continuation
        yield from line
        if newline:
            yield Segment.line()


class TerminalCodeBlock(CodeBlock):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield _syntax_text(self.text.plain, self.lexer_name)


class TerminalListItem(ListItem):
    def render_bullet(self, console: Console, options: ConsoleOptions) -> RenderResult:
        style = console.get_style("markdown.item.bullet", default="none")
        yield from _prefixed_lines(
            self.elements, console, options, Segment(" • ", style), Segment("   ", style),
        )

    def render_number(
        self, console: Console, options: ConsoleOptions, number: int, last_number: int
    ) -> RenderResult:
        width = len(str(last_number)) + 2
        style = console.get_style("markdown.item.number", default="none")
        yield from _prefixed_lines(
            self.elements, console, options,
            Segment(f"{number:>{width - 1}} ", style), Segment(" " * width, style),
        )


class TerminalBlockQuote(BlockQuote):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield from _prefixed_lines(
            self.elements, console, options, Segment("▌ ", self.style),
        )


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
        # Tables still need a bounded layout even when the surrounding transcript
        # uses terminal wrapping.
        yield from console.render(table, options.update(no_wrap=False, overflow="fold"))


class TerminalMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "heading_open": FlatHeading,
        "table_open": CompactTable,
        "fence": TerminalCodeBlock,
        "code_block": TerminalCodeBlock,
        "list_item_open": TerminalListItem,
        "blockquote_open": TerminalBlockQuote,
        "math_block": Paragraph,
    }

    def __init__(self, markup: str, *args: Any, leading_gap: bool = False, **kwargs: Any) -> None:
        # Rich has no parser injection hook; initialize its styles with empty input.
        super().__init__("", *args, **kwargs)
        self.markup = markup
        self.parsed = markdown_parser().parse(markup)
        self.leading_gap = leading_gap

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        leading_gap = self.leading_gap
        for segment in super().__rich_console__(console, options):
            if leading_gap and segment.text:
                # Lists and quotes may already start with Rich's block separator.
                if not segment.text.startswith("\n"):
                    yield Segment.line()
                leading_gap = False
            yield segment

    def _flatten_tokens(self, tokens):
        for token in super()._flatten_tokens(tokens):
            if token.type in {"math_inline", "math_block"}:
                token = copy(token)
                source = token.meta["source"]
                token.content = (
                    render_latex(token.content, source) if token.meta["closed"] else source
                )
                if token.type == "math_inline":
                    token.type = "text"
            yield token


class ToolBody:
    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield from _prefixed_lines(
            self.renderable, console, options, Segment("│ ", console.get_style("dim cyan")),
        )


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
                _syntax_text(
                    preview,
                    _lexer_for_path(path or ""),
                    line_numbers=True,
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
                _syntax_text(
                    preview or "(no textual diff)",
                    "diff",
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
                _syntax_text(
                    command,
                    "bash",
                )
            ),
        )

    if name == "read_file":
        return _tool_header("Read", path)
    if name == "read_image":
        return _tool_header("Read image", path)

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
        self._content_printed = False
        self._last_block_heading = False

    def _start_response(self) -> None:
        if self._response_started:
            return
        marker = Text("● ", style="green")
        marker.append("assistant", style="bold green")
        self.console.print(marker)
        self._response_started = True
        self._content_printed = False
        self._last_block_heading = False

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
        self.console.print(render_tool_event(event, self.cwd), soft_wrap=True)

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
            self.console.print(TerminalMarkdown(text), soft_wrap=True)
        for path in images:
            image = Text("  image  ", style="dim blue")
            image.append(_display_path(path, self.cwd), style="dim")
            self.console.print(image)

    def _history_assistant(
        self, message: dict[str, Any], show_thinking: bool
    ) -> None:
        reasoning = message.get("reasoning_content")
        if show_thinking and isinstance(reasoning, str) and reasoning:
            self.console.print(self._thinking_renderable(reasoning))

        content = message.get("content")
        if isinstance(content, str) and content:
            marker = Text("● ", style="green")
            marker.append("assistant", style="bold green")
            self.console.print(marker)
            self.console.print(TerminalMarkdown(content), soft_wrap=True)

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
            self.console.print(render_tool_event(f"{name}({arguments})", self.cwd), soft_wrap=True)

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
            self._print_content(self._pending_content.rstrip())
            self._pending_content = ""

    def _print_content(self, content: str) -> None:
        markdown = TerminalMarkdown(content)
        if not markdown.parsed:
            return
        starts_heading = markdown.parsed[0].type == "heading_open"
        if self._content_printed and (starts_heading or self._last_block_heading):
            # Restore the gap Rich would insert if these streamed blocks had
            # arrived in one Markdown document. A newline adds no copy padding.
            markdown.leading_gap = True
        self.console.print(markdown, soft_wrap=True)
        self._content_printed = True
        self._last_block_heading = markdown.parsed[-1].type == "heading_close"

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
            self._print_content(committed)

    @staticmethod
    def _completed_markdown_boundary(content: str) -> int:
        offset = 0
        boundary = 0
        protected = [
            token.map for token in markdown_parser().parse(content)
            if token.type in {"fence", "code_block", "math_block"} and token.map
        ]
        for index, line in enumerate(content.splitlines(keepends=True)):
            offset += len(line)
            if (
                not line.strip() and line.endswith(("\n", "\r"))
                and not any(start <= index < end for start, end in protected)
            ):
                boundary = offset
        return boundary

    def _thinking_renderable(self, content: str) -> Text:
        """Build a preview of *content* that always fits the viewport.

        Truncating by logical lines is not enough: reasoning commonly arrives
        as a single very long line, which wraps into hundreds of terminal
        rows.  Rendering that through ``Live`` (which forces
        ``vertical_overflow="visible"`` when stopping) scrolls the whole
        screen and corrupts the transcript.  Wrapping first and keeping only
        the most recent lines bounds the rendered height.
        """
        prefix = "◇ thinking  "
        width = max(20, self.console.width or 80)
        budget = self._thinking_line_budget()
        wrapped = self._wrap_text(content, max(10, width - len(prefix) - 1))
        if len(wrapped) > budget:
            shown = wrapped[-max(1, budget - 1) :]
            hidden = len(wrapped) - len(shown)
        else:
            shown = wrapped
            hidden = 0
        text = Text(prefix, style="magenta")
        if hidden:
            text.append(f"… {hidden} earlier lines", style="dim")
            text.append("\n", style="dim")
        text.append("\n".join(shown))
        return text

    def _thinking_line_budget(self) -> int:
        height = self.console.height or (MAX_PREVIEW_LINES + 2)
        return max(1, min(MAX_PREVIEW_LINES, height - 2))

    def _wrap_text(self, content: str, width: int) -> list[str]:
        sample = content[-MAX_PREVIEW_CHARS:]
        lines = Text(sample).wrap(self.console, max(1, width))
        return [line.plain for line in lines]

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
