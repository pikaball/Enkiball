from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from enkiball.llm_tools import TOOL_SCHEMAS, dispatch_tool
from enkiball.sandbox import Sandbox, TurnInterrupted
from enkiball.tools import MAX_EDIT_FILE_SIZE, MAX_LENGTH, edit_file, read_file, write_file


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

    def test_edit_rejects_oversized_files(self) -> None:
        self.path.write_bytes(b"x" * (MAX_EDIT_FILE_SIZE + 1))

        with self.assertRaisesRegex(ValueError, "filesize"):
            edit_file(str(self.path), "x", "y")

    def test_read_file_defaults_to_first_thousand_characters(self) -> None:
        self.path.write_text("x" * 1200, encoding="utf-8")

        self.assertEqual(read_file(str(self.path)), "x" * 1000)

    def test_read_file_uses_character_offsets(self) -> None:
        self.path.write_text("a你b好c", encoding="utf-8")

        self.assertEqual(read_file(str(self.path), start=1, end=4), "你b好")

    def test_read_file_validates_offsets(self) -> None:
        self.path.write_text("content", encoding="utf-8")

        invalid = (
            ({"start": True}, "start must be an integer"),
            ({"end": "3"}, "end must be an integer"),
            ({"start": -1}, "start must be non-negative"),
            ({"start": 3, "end": 2}, "end must be greater"),
        )
        for kwargs, error in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex((TypeError, ValueError), error):
                    read_file(str(self.path), **kwargs)

    def test_write_rejects_result_over_size_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "byte limit"):
            write_file(str(self.path), "x" * (MAX_LENGTH + 1))


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

    def test_read_dispatch_forwards_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"
            path.write_text("0123456789", encoding="utf-8")

            result = json.loads(
                dispatch_tool(
                    "read_file",
                    {"path": str(path), "start": 2, "end": 6},
                    Sandbox(cwd=temp_dir),
                )
            )

            self.assertEqual(result, {"ok": True, "content": "2345"})

    def test_read_dispatch_rejects_invalid_range(self) -> None:
        result = json.loads(
            dispatch_tool(
                "read_file",
                {"path": "unused", "start": 4, "end": 2},
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("end must be greater", result["error"])

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

    def test_dispatch_does_not_serialize_turn_interrupt(self) -> None:
        sandbox = Sandbox(cwd=Path.cwd(), approval=True)
        sandbox.set_approval_handler(
            lambda path, operation: (_ for _ in ()).throw(TurnInterrupted())
        )

        with self.assertRaises(TurnInterrupted):
            dispatch_tool(
                "read_file",
                {"path": "/definitely/outside/workspace"},
                sandbox,
            )


if __name__ == "__main__":
    unittest.main()
