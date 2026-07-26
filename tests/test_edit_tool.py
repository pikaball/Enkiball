from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from enkiball.llm_tools import TOOL_SCHEMAS, dispatch_tool
from enkiball.sandbox import Sandbox
from enkiball.tools import edit_file


class EditFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "sample.txt"

    def test_replaces_unique_match_and_preserves_mode(self) -> None:
        self.path.write_text("alpha\nbeta\n", encoding="utf-8")
        self.path.chmod(0o640)

        target, replacements = edit_file(str(self.path), "beta", "gamma")

        self.assertEqual(target, str(self.path.resolve()))
        self.assertEqual(replacements, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "alpha\ngamma\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_rejects_missing_match_without_modifying_file(self) -> None:
        original = "alpha\nbeta\n"
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "was not found"):
            edit_file(str(self.path), "missing", "replacement")

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_rejects_ambiguous_match_without_replace_all(self) -> None:
        original = "same\nsame\n"
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "matched 2 times"):
            edit_file(str(self.path), "same", "changed")

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_replace_all_reports_count(self) -> None:
        self.path.write_text("same\nsame\n", encoding="utf-8")

        _, replacements = edit_file(str(self.path), "same", "changed", replace_all=True)

        self.assertEqual(replacements, 2)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "changed\nchanged\n")

    def test_preserves_crlf_outside_replacement(self) -> None:
        self.path.write_bytes(b"alpha\r\nbeta\r\n")

        edit_file(str(self.path), "beta", "gamma")

        self.assertEqual(self.path.read_bytes(), b"alpha\r\ngamma\r\n")

    def test_rejects_empty_or_identical_search(self) -> None:
        self.path.write_text("content", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            edit_file(str(self.path), "", "new")
        with self.assertRaisesRegex(ValueError, "must be different"):
            edit_file(str(self.path), "content", "content")


class EditDispatchTests(unittest.TestCase):
    def test_schema_is_registered_once(self) -> None:
        names = [schema["function"]["name"] for schema in TOOL_SCHEMAS]
        self.assertEqual(names.count("edit"), 1)

    def test_dispatch_returns_structured_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"
            path.write_text("before", encoding="utf-8")

            result = json.loads(
                dispatch_tool(
                    "edit",
                    {"path": str(path), "old_text": "before", "new_text": "after"},
                    Sandbox(cwd=temp_dir),
                )
            )

            self.assertEqual(
                result,
                {"ok": True, "path": str(path.resolve()), "replacements": 1},
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "after")

    def test_dispatch_rejects_non_boolean_replace_all(self) -> None:
        result = json.loads(
            dispatch_tool(
                "edit",
                {
                    "path": "unused",
                    "old_text": "before",
                    "new_text": "after",
                    "replace_all": "false",
                },
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("must be a boolean", result["error"])

    def test_dispatch_rejects_non_string_fields(self) -> None:
        result = json.loads(
            dispatch_tool(
                "edit",
                {"path": None, "old_text": "before", "new_text": "after"},
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("path must be a string", result["error"])


if __name__ == "__main__":
    unittest.main()
