from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from enkiball.session_store import SessionStore
from enkiball.system_cli.app import main
from enkiball.system_cli.session_search import search_sessions


class SessionSearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.store = SessionStore._from_paths(
            self.root / "sessions", self.root / "exports", cwd=self.cwd,
        )

    def create(self, name, messages, cwd=None):
        store = SessionStore._from_paths(
            self.store.base_dir, self.store.export_dir, cwd=cwd or self.cwd,
        )
        return store.create(name, messages)

    def names(self, keyword="needle", **options):
        return {result.record.name for result in search_sessions(self.store, keyword, **options)}

    def test_default_fields_and_independent_include_flags(self):
        cases = {
            "user": [{"role": "user", "content": "A needle in the text"}],
            "assistant": [{"role": "assistant", "content": "NEEDLE in answer"}],
            "multimodal": [{"role": "user", "content": [
                {"type": "text", "text": "needle description"},
                {"type": "input_image", "path": "/image.png"},
            ]}],
            "tool name": [{"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "needle_tool", "arguments": "{}"}},
            ]}],
            "tool arguments": [{"role": "assistant", "tool_calls": [
                {"function": {"name": "read", "arguments": '{"path": "needle.py"}'}},
            ]}],
            "tool result": [{"role": "tool", "content": '{"output": "needle"}'}],
            "thinking": [{"role": "assistant", "content": "answer", "reasoning_content": "needle thought"}],
            "system": [{"role": "system", "content": "needle instruction"}],
            "metadata": [{"role": "assistant", "content": None, "_provider": {"text": "needle"},
                          "tool_calls": [{"id": "needle", "function": {"name": "read"}}]}],
            "image": [{"role": "user", "content": [{"type": "input_image", "path": "/needle.png"}]}],
            "tool image": [{"role": "tool", "content": [{"type": "image_url", "image_url": {"url": "needle"}}]}],
        }
        for name, messages in cases.items():
            self.create(name, messages)
        self.create("needle title", [])
        defaults = {"user", "assistant", "multimodal", "needle title"}
        tools = {"tool name", "tool arguments", "tool result"}
        self.assertEqual(self.names(), defaults)
        self.assertEqual(self.names(include_tool=True), defaults | tools)
        self.assertEqual(self.names(include_thinking=True), defaults | {"thinking"})
        self.assertEqual(self.names(include_tool=True, include_thinking=True), defaults | tools | {"thinking"})

    def test_scope_is_exact_cwd_and_global_includes_legacy_records(self):
        for name, cwd in (("current", self.cwd), ("parent", self.root),
                          ("child", self.cwd / "subdir"), ("sibling", self.root / "other")):
            self.create(name, [{"role": "user", "content": "needle"}], cwd=cwd)
        (self.store.base_dir / "legacy.json").write_text(json.dumps({
            "session_id": "legacy", "name": "legacy", "messages": [{"role": "user", "content": "needle"}],
        }))
        self.assertEqual(self.names(), {"current"})
        self.assertEqual(self.names(global_search=True), {"current", "parent", "child", "sibling", "legacy"})
        # Existing session listing still has its original ancestor visibility.
        self.assertEqual({record.name for record in self.store.list_sessions()}, {"current", "parent"})

    def test_matching_is_literal_case_insensitive_and_decodes_tool_json(self):
        self.create("literal", [{"role": "user", "content": "literal A.B [brackets] 中文"}])
        self.create("not regex", [{"role": "assistant", "content": "axb"}])
        self.create("escaped tool", [{"role": "assistant", "tool_calls": [
            {"function": {"name": "write", "arguments": json.dumps({"text": "中文"})}},
        ]}])
        self.create("structured tool", [{"role": "assistant", "tool_calls": [
            {"function": {"name": "write", "arguments": {"text": "中文"}}},
        ]}])
        self.assertEqual(self.names("a.b"), {"literal"})
        self.assertEqual(self.names("[brackets]"), {"literal"})
        self.assertEqual(self.names("中文"), {"literal"})
        self.assertEqual(self.names("中文", include_tool=True), {"literal", "escaped tool", "structured tool"})

    def test_results_are_newest_first_unique_and_snippet_contains_hit(self):
        content = "before " * 100 + "needle" + " after" * 100
        for day, name in ((1, "old"), (3, "new"), (2, "middle")):
            with patch("enkiball.session_store._now_iso", return_value=f"2026-11-{day:02d}"):
                self.create(name, [{"role": "user", "content": content},
                                   {"role": "assistant", "content": "another needle"}])
        results = search_sessions(self.store, "needle")
        self.assertEqual([result.record.name for result in results], ["new", "middle", "old"])
        self.assertEqual(len({result.record.session_id for result in results}), 3)
        for result in results:
            self.assertEqual(result.source, "user")
            self.assertIn("needle", result.snippet)
            self.assertTrue(result.snippet.startswith("…"))
            self.assertTrue(result.snippet.endswith("…"))
            self.assertLess(len(result.snippet), 180)

    def test_corrupt_records_and_malformed_message_parts_do_not_break_search(self):
        (self.store.base_dir / "broken.json").write_text("{broken")
        (self.store.base_dir / "not-a-record.json").write_text("[]")
        self.create("valid", [None, "invalid", {"role": "assistant", "tool_calls": [None, {}]},
                              {"role": "user", "content": [None, {"type": "text", "text": "needle"}]}])
        self.assertEqual(self.names(include_tool=True, include_thinking=True), {"valid"})

    def test_memory_store_global_listing_does_not_change_scoped_listing(self):
        store = SessionStore._memory(cwd=self.cwd)
        record = store.create("local", [{"role": "user", "content": "needle"}])
        store.cwd = self.root / "other"
        self.assertEqual(search_sessions(store, "needle"), [])
        self.assertEqual(search_sessions(store, "needle", global_search=True)[0].record.session_id, record.session_id)
        self.assertEqual(store.list_sessions(), [])

    def test_search_command_is_offline_read_only_and_prints_resumable_ids(self):
        local = self.create("[red]literal title[/red]", [{"role": "user", "content": "needle text"}])
        remote = self.create("remote", [{"role": "tool", "content": "needle result"}], cwd=self.root / "other")
        thinking = self.create("reasoning", [{"role": "assistant", "reasoning_content": "needle thought"}])
        before = {path: path.read_bytes() for path in self.store.base_dir.iterdir()}
        starting_cwd = Path.cwd()
        output = io.StringIO()
        with patch("enkiball.session_store.SESSION_DIR", self.store.base_dir), patch(
            "enkiball.session_store.EXPORT_DIR", self.store.export_dir,
        ), patch("enkiball.system_cli.app.Console", return_value=Console(file=output, width=200)), patch(
            "enkiball.system_cli.app.Enkiball",
        ) as agent, patch("enkiball.system_cli.app.EnkiballCLI") as cli:
            main(["search", "needle", "--cwd", str(self.cwd), "--config", "missing.json"])
            self.assertIn(local.session_id, output.getvalue())
            self.assertIn(local.name, output.getvalue())
            self.assertNotIn(remote.session_id, output.getvalue())
            self.assertNotIn(thinking.session_id, output.getvalue())
            output.seek(0)
            output.truncate()
            main(["--cwd", str(self.cwd), "search", "needle", "--global", "--include-tool", "--include-thinking"])
            for record in (local, remote, thinking):
                self.assertIn(record.session_id, output.getvalue())
                self.assertIn(str(record.cwd), output.getvalue())
            output.seek(0)
            output.truncate()
            main(["search", "not found", "--cwd", str(self.cwd)])
            self.assertIn("No matching sessions", output.getvalue())
            agent.assert_not_called()
            cli.assert_not_called()
        self.assertEqual(Path.cwd(), starting_cwd)
        self.assertEqual({path: path.read_bytes() for path in self.store.base_dir.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
