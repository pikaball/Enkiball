from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from enkiball.cli_renderer import CliRenderer, parse_tool_event, render_tool_event


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

        renderer.start()
        renderer.token("# Heading\n\n")
        renderer.token("- first\n- second")
        renderer.finish()
        output = console.export_text()

        self.assertIn("assistant", output)
        self.assertIn("Heading", output)
        self.assertIn("first", output)
        self.assertIn("second", output)


if __name__ == "__main__":
    unittest.main()
