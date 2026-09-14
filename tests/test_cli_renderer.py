from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from enkiball.cli_renderer import (
    CliRenderer,
    TerminalMarkdown,
    parse_tool_event,
    render_tool_event,
)


class ToolEventRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = Path(self.temp_dir.name)

    def _render(self, name: str, args: dict[str, object]) -> str:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=100,
            color_system=None,
        )
        event = f"{name}({json.dumps(args)})"
        console.print(render_tool_event(event, self.cwd))
        return console.export_text()

    def test_parse_tool_event_handles_valid_and_malformed_events(self) -> None:
        self.assertEqual(
            parse_tool_event('edit({"path": "a.py"})'),
            ("edit", {"path": "a.py"}),
        )
        self.assertEqual(parse_tool_event("invalid"), ("invalid", {}))

    def test_write_renders_path_and_content_preview(self) -> None:
        output = self._render(
            "write_file",
            {"path": "src/app.py", "content": "print('hello')\n"},
        )

        self.assertIn("Write", output)
        self.assertIn("src/app.py", output)
        self.assertIn("print('hello')", output)

    def test_write_truncates_large_content_preview(self) -> None:
        output = self._render(
            "write_file",
            {"path": "large.txt", "content": "\n".join(str(i) for i in range(30))},
        )

        self.assertIn("6 more lines", output)
        self.assertNotIn("\n29\n", output)

    def test_edit_renders_unified_diff(self) -> None:
        output = self._render(
            "edit",
            {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
        )

        self.assertIn("Edit", output)
        self.assertIn("--- a/app.py", output)
        self.assertIn("-value = 1", output)
        self.assertIn("+value = 2", output)

    def test_command_renders_without_other_sensitive_arguments(self) -> None:
        output = self._render(
            "run_command",
            {"command": "python -m unittest", "timeout_sec": 30},
        )

        self.assertIn("Run", output)
        self.assertIn("timeout 30s", output)
        self.assertIn("python -m unittest", output)

    def test_privileged_command_is_clearly_labeled(self) -> None:
        output = self._render(
            "run_command",
            {"command": "id", "privilleged": True},
        )

        self.assertIn("Run privileged", output)

    def test_read_uses_relative_workspace_path(self) -> None:
        output = self._render("read_file", {"path": str(self.cwd / "README.md")})

        self.assertIn("Read", output)
        self.assertIn("README.md", output)
        self.assertNotIn(str(self.cwd), output)


class MarkdownRenderingTests(unittest.TestCase):
    def test_streamed_markdown_is_rendered_and_finalized(self) -> None:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=80,
            color_system=None,
            force_terminal=False,
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.token("# Heading\n\n")
        renderer.token("- first\n- second")
        renderer.finish()
        output = console.export_text()

        self.assertIn("assistant", output)
        self.assertIn("Heading", output)
        self.assertIn("first", output)
        self.assertIn("second", output)
        self.assertNotIn("━", output)

    def test_heading_is_left_aligned_without_banner(self) -> None:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=60,
            color_system=None,
            force_terminal=False,
        )
        console.print(TerminalMarkdown("# Left heading"))
        lines = console.export_text().splitlines()

        self.assertEqual(lines[0].rstrip(), "Left heading")
        self.assertNotIn("━", "".join(lines))

    def test_table_uses_flat_full_width_layout(self) -> None:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=80,
            color_system=None,
            force_terminal=False,
        )
        console.print(
            TerminalMarkdown(
            "| Name | Status | Detail |\n"
            "| --- | --- | --- |\n"
            "| build | pass | all checks completed successfully |"
            )
        )
        output = console.export_text()

        self.assertIn("Name", output)
        self.assertIn("Status", output)
        self.assertIn("all checks completed successfully", output)
        self.assertNotIn("┃", output)

    def test_incomplete_streaming_table_is_not_committed_early(self) -> None:
        incomplete = "| Name | Status |\n| ---"
        complete = "| Name | Status |\n| --- | --- |\n| build | pass |\n\n"

        self.assertEqual(CliRenderer._completed_markdown_boundary(incomplete), 0)
        self.assertEqual(
            CliRenderer._completed_markdown_boundary(complete), len(complete)
        )

    def test_approval_displays_once_always_and_deny(self) -> None:
        console = Console(
            file=io.StringIO(), record=True, width=80, color_system=None
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.approval(Path("/outside"), "edit")
        output = console.export_text()

        self.assertIn("1. once", output)
        self.assertIn("2. always", output)
        self.assertIn("3. deny", output)
        self.assertIn("Esc", output)
        self.assertIn("interrupt this turn", output)

    def test_privileged_approval_does_not_offer_always(self) -> None:
        console = Console(
            file=io.StringIO(), record=True, width=80, color_system=None
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.approval(Path.cwd(), "privileged command")
        output = console.export_text()

        self.assertIn("1. allow", output)
        self.assertIn("2. deny", output)
        self.assertNotIn("always", output)
        self.assertIn("outside the sandbox", output)

    def test_response_after_tool_gets_a_new_assistant_marker(self) -> None:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=80,
            color_system=None,
            force_terminal=False,
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.token("Before")
        renderer.tool('read_file({"path": "README.md"})')
        renderer.token("After")
        renderer.finish()
        output = console.export_text()

        self.assertEqual(output.count("assistant"), 2)
        self.assertLess(output.index("Before"), output.index("Read"))
        self.assertLess(output.index("Read"), output.index("After"))

    def test_tool_first_does_not_render_empty_assistant_marker(self) -> None:
        console = Console(
            file=io.StringIO(), record=True, width=80, color_system=None
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.tool('read_file({"path": "README.md"})')
        output = console.export_text()

        self.assertNotIn("assistant", output)

    @unittest.skipUnless(shutil.which("script"), "script utility is not installed")
    def test_long_response_is_not_duplicated_in_small_terminal(self) -> None:
        text = "\n\n".join(
            f"paragraph-{index}: long response content" for index in range(18)
        )
        code = (
            "from pathlib import Path; "
            "from rich.console import Console; "
            "from enkiball.cli_renderer import CliRenderer; "
            f"text={text!r}; "
            "renderer=CliRenderer(Console(force_terminal=True), Path.cwd()); "
            "renderer.token(text); renderer.finish()"
        )
        env = dict(os.environ)
        env.update({"COLUMNS": "60", "LINES": "8", "PYTHONPATH": "src"})

        result = subprocess.run(
            ["script", "-q", "-c", f"{sys.executable} -c {json.dumps(code)}", "/dev/null"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)

        self.assertEqual(output.count("paragraph-0:"), 1)
        self.assertEqual(output.count("paragraph-17:"), 1)

    def test_thinking_preview_is_bounded_in_small_terminal(self) -> None:
        console = Console(
            file=io.StringIO(),
            width=60,
            height=8,
            color_system=None,
            force_terminal=False,
        )
        renderer = CliRenderer(console, Path.cwd())

        preview = renderer._thinking_renderable("word " * 5000).plain
        lines = preview.splitlines()

        self.assertIn("thinking", preview)
        self.assertIn("earlier lines", preview)
        self.assertLessEqual(len(lines), 6)

    def test_thinking_preview_keeps_recent_lines(self) -> None:
        console = Console(
            file=io.StringIO(),
            width=80,
            height=24,
            color_system=None,
            force_terminal=False,
        )
        renderer = CliRenderer(console, Path.cwd())

        preview = renderer._thinking_renderable("first\nsecond\nthird").plain

        self.assertIn("third", preview)
        self.assertNotIn("earlier lines", preview)

    def test_finish_is_idempotent_after_interruption(self) -> None:
        console = Console(
            file=io.StringIO(),
            record=True,
            width=80,
            color_system=None,
            force_terminal=False,
        )
        renderer = CliRenderer(console, Path.cwd())

        renderer.token("partial interrupted response")
        renderer.finish()
        renderer.finish()
        output = console.export_text()

        self.assertEqual(output.count("partial interrupted response"), 1)


class SessionHistoryRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        self.console = Console(
            file=self.output,
            record=True,
            width=90,
            color_system=None,
            force_terminal=False,
        )
        self.renderer = CliRenderer(self.console, Path.cwd())

    def test_history_reuses_markdown_and_tool_rendering(self) -> None:
        messages = [
            {"role": "system", "content": "hidden system prompt"},
            {"role": "user", "content": "# Request\n\nInspect the file."},
            {
                "role": "assistant",
                "content": "| Item | State |\n| --- | --- |\n| file | ready |",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "path": "app.py",
                                    "old_text": "x = 1",
                                    "new_text": "x = 2",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"ok": true, "secret": "do not replay"}',
            },
            {"role": "assistant", "content": "Done."},
        ]

        self.renderer.history(messages, title="restored")
        output = self.console.export_text()

        self.assertIn("session  restored", output)
        self.assertIn("● you", output)
        self.assertIn("Request", output)
        self.assertIn("● assistant", output)
        self.assertIn("Item", output)
        self.assertIn("ready", output)
        self.assertIn("└─ Edit  app.py", output)
        self.assertIn("-x = 1", output)
        self.assertIn("Done.", output)
        self.assertNotIn("hidden system prompt", output)
        self.assertNotIn("do not replay", output)

    def test_history_renders_user_images_without_raw_payload(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {"type": "input_image", "path": str(Path.cwd() / "shot.png")},
                ],
            }
        ]

        self.renderer.history(messages, title="images")
        output = self.console.export_text()

        self.assertIn("Describe this", output)
        self.assertIn("image  shot.png", output)
        self.assertNotIn("input_image", output)

    def test_history_thinking_respects_display_setting(self) -> None:
        messages = [
            {
                "role": "assistant",
                "reasoning_content": "private reasoning",
                "content": "Answer",
            }
        ]

        self.renderer.history(messages, title="hidden", show_thinking=False)
        hidden = self.console.export_text()
        self.assertNotIn("private reasoning", hidden)

        output = io.StringIO()
        console = Console(file=output, record=True, width=90, color_system=None)
        CliRenderer(console, Path.cwd()).history(
            messages, title="shown", show_thinking=True
        )
        shown = console.export_text()
        self.assertIn("private reasoning", shown)


if __name__ == "__main__":
    unittest.main()
