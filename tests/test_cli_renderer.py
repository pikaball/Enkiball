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
from unittest.mock import patch

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


class HeadingSpacingTests(unittest.TestCase):
    def test_heading_gaps_are_independent_of_stream_chunk_size(self) -> None:
        for heading in ("# Title", "## Title", "###### Title", "Title\n-----"):
            source = f"Before\n\n{heading}\n\nAfter"
            expected = "● assistant\nBefore\n\nTitle\n\nAfter\n"
            for size in (1, 5, len(source)):
                with self.subTest(heading=heading, size=size):
                    output = io.StringIO()
                    renderer = CliRenderer(Console(file=output, width=20, color_system=None), Path.cwd())
                    for offset in range(0, len(source), size):
                        renderer.token(source[offset:offset + size])
                    renderer.finish()
                    self.assertEqual(output.getvalue(), expected)

    def test_consecutive_headings_have_one_empty_line_without_padding(self) -> None:
        output = io.StringIO()
        renderer = CliRenderer(Console(file=output, width=20, color_system=None), Path.cwd())
        for part in ("# First\n\n", "## Second\n\n", "- item\n\n", "### Third"):
            renderer.token(part)
        renderer.finish()
        self.assertEqual(output.getvalue(), "● assistant\nFirst\n\nSecond\n\n • item\n\nThird\n")

    def test_heading_spacing_resets_after_tool(self) -> None:
        output = io.StringIO()
        renderer = CliRenderer(Console(file=output, color_system=None), Path.cwd())
        renderer.token("# Title\n\n")
        renderer.tool('read_file({"path": "README.md"})')
        renderer.token("After")
        renderer.finish()
        self.assertTrue(output.getvalue().endswith("● assistant\nAfter\n"))


class LatexRenderingTests(unittest.TestCase):
    def render(self, source: str, width: int = 80) -> str:
        output = io.StringIO()
        Console(file=output, width=width, color_system=None).print(
            TerminalMarkdown(source), soft_wrap=True,
        )
        return output.getvalue()

    def test_inline_math_delimiters_and_common_notation(self) -> None:
        cases = (
            (r"Energy $E=mc^2$.", "Energy E=mc².\n"),
            (r"Value \(x_i + y^{n+1}\).", "Value xᵢ + yⁿ⁺¹.\n"),
            (r"$\alpha + \beta$", "α+ β\n"),
            (r"$\frac{a+b}{c+d}$", "(a+b)/(c+d)\n"),
            (r"$\sqrt{x}$", "√(x)\n"),
            (r"$\left(x+1\right)^2$", "(x+1)²\n"),
            (r"$x^{a+b}$", "x^(a+b)\n"),
            (r"**Value:** $a_i*b_j$", "Value: aᵢ*bⱼ\n"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(self.render(source), expected)

    def test_display_math_and_surrounding_markdown(self) -> None:
        for opening, closing in (("$$", "$$"), (r"\[", r"\]")):
            with self.subTest(opening=opening):
                self.assertEqual(
                    self.render(f"Before\n\n{opening}\nx^2\n{closing}\n\n**After**"),
                    "Before\n\nx²\n\nAfter\n",
                )
                self.assertEqual(self.render(f"{opening}x^2{closing}"), "x²\n")
                self.assertEqual(self.render(f"Before {opening}x^2{closing} after"), "Before x² after\n")

    def test_invalid_or_unsupported_math_keeps_the_entire_source(self) -> None:
        for expression in (r"\unknown{a_b*2}", r"\frac{1}", r"\frac{1}{", "x^", r"\input{file}"):
            for opening, closing in (("$", "$"), (r"\(", r"\)"), ("$$", "$$"), (r"\[", r"\]")):
                source = opening + expression + closing
                with self.subTest(source=source):
                    self.assertEqual(self.render(source), source + "\n")

    def test_unclosed_formulas_remain_literal(self) -> None:
        for source in (r"$\frac{a_b}{2}", r"\(\alpha * x_2", "$$\n\\alpha\n\n* x_2", "\\[\n\\alpha\n\n* x_2"):
            with self.subTest(source=source):
                self.assertEqual(self.render(source), source + "\n")

    def test_conversion_exception_falls_back_without_losing_other_content(self) -> None:
        with patch("enkiball.cli_math._MathText.nodelist_to_text", side_effect=RuntimeError("failed")):
            self.assertEqual(self.render(r"Before $\alpha$ **after**"), "Before $\\alpha$ after\n")

    def test_multiline_fallback_keeps_blank_lines_and_indentation(self) -> None:
        source = "$$\n  \\unknown{a_b}\n\n    * x\n$$"
        self.assertEqual(self.render(source), source + "\n")

    def test_code_currency_and_escaped_delimiters_are_not_converted(self) -> None:
        cases = (
            (r"`$\alpha + x_2$`", "$\\alpha + x_2$\n"),
            ("```latex\n$\\alpha + x_2$\n\n$$\n```", "$\\alpha + x_2$\n\n$$\n"),
            ("    $\\alpha + x_2$", "$\\alpha + x_2$\n"),
            (r"Costs $5 and $10.", "Costs $5 and $10.\n"),
            (r"Costs $5 **today**.", "Costs $5 today.\n"),
            (r"\$x_2\$", "$x_2$\n"),
            (r"\\(x_2\\)", "\\(x_2\\)\n"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(self.render(source), expected)

    def test_math_works_in_headings_lists_tables_and_links(self) -> None:
        source = "# $x^2$\n\n- $x_1$\n\n| Value |\n| --- |\n| $x_2$ |\n\n[Link $x^3$](https://example.test/$value)"
        output = self.render(source)
        for value in ("x²", " • x₁", "x₂", "Link x³"):
            self.assertIn(value, output)

    def test_streaming_math_waits_through_internal_blank_lines(self) -> None:
        for opening, closing in (("$$", "$$"), (r"\[", r"\]")):
            output = io.StringIO()
            renderer = CliRenderer(Console(file=output, color_system=None), Path.cwd())
            for part in ("# Formula\n\n", opening + "\n", "x^2\n\n"):
                renderer.token(part)
            self.assertEqual(output.getvalue(), "● assistant\nFormula\n")
            for char in "+ y^2\n" + closing + "\n\n":
                renderer.token(char)
            renderer.finish()
            self.assertEqual(output.getvalue(), "● assistant\nFormula\n\nx²\n\n+ y²\n")

    def test_interrupted_math_is_flushed_once_as_source(self) -> None:
        output = io.StringIO()
        renderer = CliRenderer(Console(file=output, color_system=None), Path.cwd())
        source = "$$\n\\alpha\n\n+ x_2"
        renderer.token(source)
        renderer.finish()
        renderer.finish()
        self.assertEqual(output.getvalue(), "● assistant\n" + source + "\n")

    def test_history_and_streaming_share_math_rendering(self) -> None:
        source = "# Formula\n\nValue $x^2$"
        expected = self.render(source)
        output = io.StringIO()
        renderer = CliRenderer(Console(file=output, color_system=None), Path.cwd())
        renderer.history([{"role": "assistant", "content": source}], title="math")
        self.assertIn("● assistant\n" + expected, output.getvalue())
        output.seek(0)
        output.truncate()
        for char in source:
            renderer.token(char)
        renderer.finish()
        self.assertEqual(output.getvalue(), "● assistant\n" + expected)

    def test_long_math_does_not_insert_viewport_padding_or_newlines(self) -> None:
        expression = " + ".join(f"x_{{{index}}}" for index in range(20))
        output = self.render("$" + expression + "$", width=20)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("x₁₉", output)
        self.assertEqual(output.rstrip() + "\n", output)


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


class CopyableTranscriptTests(unittest.TestCase):
    def renderer(self, width=24):
        output = io.StringIO()
        console = Console(file=output, width=width, color_system=None)
        return CliRenderer(console, Path.cwd()), output

    def test_paragraphs_do_not_insert_viewport_newlines_or_padding(self):
        samples = [
            "https://example.test/" + "long-path/" * 12,
            "words separated by spaces " * 8,
            "中文文字与 English 混排，保持原来的空格。" * 8,
        ]
        for width in (20, 60):
            for sample in samples:
                for completed_block in (False, True):
                    with self.subTest(width=width, sample=sample, completed_block=completed_block):
                        text = sample.rstrip()
                        renderer, output = self.renderer(width)
                        renderer.token(text[:10])
                        renderer.token(text[10:] + ("\n\n" if completed_block else ""))
                        renderer.finish()
                        self.assertEqual(output.getvalue(), "● assistant\n" + text + "\n")

    def test_code_preserves_source_lines_and_indentation(self):
        code = (
            '    curl "https://example.test/' + "long-path/" * 10 + '" \\\n'
            '      --header "X-Long-Value: ' + "abcdef" * 20 + '"\n'
            '\n    echo "done"  \n'
        )
        renderer, output = self.renderer()
        renderer.token("```sh\n" + code + "```")
        renderer.finish()
        self.assertEqual(output.getvalue(), "● assistant\n" + code)

    def test_lists_and_quotes_keep_long_items_on_one_logical_line(self):
        text = "long list item with inline code " + "identifier" * 12
        samples = (
            ("- " + text, " • " + text),
            ("10. " + text, " 10 " + text),
            ("> " + text, "▌ " + text),
            ("- parent\n  - " + text, "    • " + text),
        )
        for source, expected in samples:
            with self.subTest(source=source):
                renderer, output = self.renderer()
                renderer.token(source)
                renderer.finish()
                self.assertIn(expected, output.getvalue().splitlines())
                self.assertEqual(output.getvalue().count(text), 1)

    def test_markdown_hard_breaks_remain_separate_lines(self):
        renderer, output = self.renderer()
        renderer.token("first line  \nsecond line")
        renderer.finish()
        self.assertEqual(output.getvalue(), "● assistant\nfirst line\nsecond line\n")

    def test_tool_commands_and_file_previews_do_not_repeat_wrap_prefixes(self):
        command = "curl https://example.test/" + "long-path/" * 12
        content = 'value = "' + "abcdef" * 20 + '"\n    next_line()\n'
        cases = (
            ("run_command", {"command": command}, ["│ " + command]),
            ("write_file", {"path": "demo.py", "content": content},
             ["│ 1 " + content.splitlines()[0], "│ 2     next_line()"]),
            ("edit", {"path": "demo.py", "old_text": "before", "new_text": content},
             ["│ +" + content.splitlines()[0], "│ +    next_line()"]),
        )
        for name, args, expected in cases:
            with self.subTest(name=name):
                renderer, output = self.renderer()
                renderer.tool(f"{name}({json.dumps(args)})")
                for line in expected:
                    self.assertIn(line, output.getvalue().splitlines())

    def test_history_uses_the_same_logical_lines(self):
        text = "history line " + "abcdefghij" * 12
        command = "echo " + "abcdef" * 20
        renderer, output = self.renderer()
        renderer.history([
            {"role": "user", "content": text},
            {"role": "assistant", "content": text, "tool_calls": [{
                "function": {"name": "run_command", "arguments": json.dumps({"command": command})},
            }]},
        ], title="test")
        self.assertEqual(output.getvalue().splitlines().count(text), 2)
        self.assertIn("│ " + command, output.getvalue().splitlines())

    def test_tables_remain_bounded_when_prose_uses_terminal_wrapping(self):
        renderer, output = self.renderer(width=30)
        renderer.token("| Key | Value |\n| --- | --- |\n| item | " + "abcdef" * 20 + " |")
        renderer.finish()
        lines = output.getvalue().splitlines()
        self.assertTrue(all(len(line) <= 30 for line in lines))
        cell_text = output.getvalue().split("item", 1)[1]
        self.assertEqual("".join(cell_text.split()), "abcdef" * 20)


if __name__ == "__main__":
    unittest.main()
