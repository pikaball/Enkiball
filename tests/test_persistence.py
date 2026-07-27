from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball import Enkiball, EnkiballCLI
from enkiball.config import (
    DEFAULT_STATE_PATH,
    load_config,
    save_config,
    save_state,
)
from enkiball.session_store import SessionRecord, SessionStore


class SessionStorePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.store = SessionStore._from_paths(
            self.root / "sessions", self.root / "exports"
        )

    def test_session_operations_reject_path_traversal(self) -> None:
        for session_id in ("../outside", "nested/session", "/tmp/outside", "."):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.store.load(session_id)
                with self.assertRaises(ValueError):
                    self.store.save_messages(session_id, [])
                with self.assertRaises(ValueError):
                    self.store.export_session(session_id)

    def test_session_path_rejects_symlink_escape(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        (self.store.base_dir / "linked.json").symlink_to(outside)

        with self.assertRaises(ValueError):
            self.store.load("linked")


class AtomicJsonWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def assert_replacement_failure_preserves_file(self, write: object, path: Path) -> None:
        original = '{"original": true}\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original, encoding="utf-8")

        with patch("enkiball.persistence.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                write()

        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_config_write_is_atomic(self) -> None:
        path = self.root / "config" / "enki.json"
        self.assert_replacement_failure_preserves_file(
            lambda: save_config({"updated": True}, path), path
        )

    def test_state_write_is_atomic(self) -> None:
        path = self.root / "state" / "state.json"
        self.assert_replacement_failure_preserves_file(
            lambda: save_state({"active_model": "new"}, path), path
        )

    def test_session_write_is_atomic(self) -> None:
        store = SessionStore._from_paths(
            self.root / "sessions", self.root / "exports"
        )
        path = store.base_dir / "valid-id.json"
        record = SessionRecord(
            session_id="valid-id",
            name="valid",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            messages=[],
        )
        self.assert_replacement_failure_preserves_file(lambda: store.save(record), path)


class StatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "enki.json"
        save_config(
            {
                "providers": {
                    "first": {"models": {"one": {"max_tokens": 100}}},
                    "second": {"models": {"two": {"max_tokens": 200}}},
                }
            },
            self.config_path,
        )

    def build_agent(self, state_path: Path) -> Enkiball:
        return Enkiball(
            config_path=self.config_path,
            state_path=state_path,
            session_dir=state_path.parent / f"{state_path.stem}-sessions",
            auto_start_mcp=False,
        )

    def test_public_constructor_default_remains_default_state_path(self) -> None:
        default = inspect.signature(Enkiball).parameters["state_path"].default
        self.assertEqual(default, DEFAULT_STATE_PATH)

    def test_custom_agents_isolate_provider_and_model_state(self) -> None:
        first_path = self.root / "agent-one" / "state.json"
        second_path = self.root / "agent-two" / "state.json"
        first = self.build_agent(first_path)
        second = self.build_agent(second_path)

        self.assertTrue(first.switch_provider("second"))
        first.model = "custom-one"
        second.model = "custom-two"

        self.assertEqual(first.provider, "second")
        self.assertEqual(first.model, "custom-one")
        self.assertEqual(second.provider, "first")
        self.assertEqual(second.model, "custom-two")
        self.assertEqual(first._engine.state_path, first_path)
        self.assertEqual(second._engine.state_path, second_path)

        first.model = "two"
        second.model = "one"
        self.assertEqual(first._engine._auto_compact_effective_threshold_tokens(), 140)
        self.assertEqual(second._engine._auto_compact_effective_threshold_tokens(), 70)

    def test_command_handler_uses_agent_state_path(self) -> None:
        state_path = self.root / "cli-agent" / "state.json"
        agent = self.build_agent(state_path)
        cli = EnkiballCLI(agent)

        cli.handler._model(["/model", "command-model"], agent._engine)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["active_model"], "command-model")
        self.assertEqual(cli.handler.state_path, state_path)

    def test_legacy_migration_preserves_unknown_fields(self) -> None:
        state_path = self.root / "legacy-state.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "llm": {
                        "provider": "legacy",
                        "model": "old-model",
                        "api_key": "key",
                    },
                    "system_prompt": "prompt",
                    "plugin_options": {"enabled": True},
                    "future_field": [1, 2, 3],
                }
            ),
            encoding="utf-8",
        )

        migrated = load_config(self.config_path, state_path)

        self.assertEqual(migrated["plugin_options"], {"enabled": True})
        self.assertEqual(migrated["future_field"], [1, 2, 3])
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["plugin_options"], {"enabled": True})
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")),
            {"active_provider": "legacy", "active_model": "old-model"},
        )


if __name__ == "__main__":
    unittest.main()
