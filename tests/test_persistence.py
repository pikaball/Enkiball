from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball import Enkiball, EnkiballCLI
from enkiball.config import (
    DEFAULT_CONFIG,
    DEFAULT_STATE_PATH,
    ensure_config,
    load_config,
    load_state,
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

    def test_sessions_are_filtered_to_current_cwd_and_ancestors(self) -> None:
        sessions = self.root / "sessions"
        exports = self.root / "exports"
        project = self.root / "project"
        child = project / "src"
        sibling = self.root / "sibling"
        nested = child / "nested"
        for directory in (project, child, sibling, nested):
            directory.mkdir(parents=True, exist_ok=True)

        project_store = SessionStore._from_paths(sessions, exports, cwd=project)
        project_record = project_store.create("project")
        child_store = SessionStore._from_paths(sessions, exports, cwd=child)
        child_record = child_store.create("child")
        sibling_store = SessionStore._from_paths(sessions, exports, cwd=sibling)
        sibling_store.create("sibling")
        nested_store = SessionStore._from_paths(sessions, exports, cwd=nested)
        nested_store.create("nested")

        visible = child_store.list_sessions()

        self.assertEqual(
            {record.session_id for record in visible},
            {project_record.session_id, child_record.session_id},
        )
        self.assertEqual(child_store.resolve("1"), visible[0].session_id)
        with self.assertRaises(PermissionError):
            child_store.load(sibling_store.current_session_id or "")

    def test_saving_ancestor_session_preserves_its_bound_cwd(self) -> None:
        sessions = self.root / "sessions"
        exports = self.root / "exports"
        project = self.root / "project"
        child = project / "child"
        child.mkdir(parents=True)
        parent_store = SessionStore._from_paths(sessions, exports, cwd=project)
        record = parent_store.create("parent")
        child_store = SessionStore._from_paths(sessions, exports, cwd=child)

        loaded = child_store.load(record.session_id)
        loaded.messages.append({"role": "user", "content": "hello"})
        child_store.save(loaded)

        persisted = json.loads(
            (sessions / f"{record.session_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["cwd"], str(project.resolve()))

    def test_legacy_session_without_cwd_is_not_visible(self) -> None:
        legacy = self.store.base_dir / "legacy.json"
        legacy.write_text(
            json.dumps(
                {
                    "session_id": "legacy",
                    "name": "legacy",
                    "messages": [],
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.store.list_sessions(), [])
        with self.assertRaises(PermissionError):
            self.store.load("legacy")


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
                    "first": {"models": {"one": {"max_tokens": 100, "context_window": 1000}}},
                    "second": {"models": {"two": {"max_tokens": 200, "context_window": 2000}}},
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

    def test_fresh_config_selects_a_model_without_a_state_file(self) -> None:
        agent = self.build_agent(self.root / "fresh-state.json")
        self.addCleanup(agent.shutdown)
        self.assertEqual(agent.provider, "first")
        self.assertEqual(agent.model, "one")
        self.assertEqual(agent._engine._build_stream_request({})["model"], "one")

    def test_saved_remote_only_model_is_not_replaced_by_config_default(self) -> None:
        state_path = self.root / "remote-state.json"
        save_state({"active_provider": "second", "active_model": "remote-only"}, state_path)
        agent = self.build_agent(state_path)
        self.addCleanup(agent.shutdown)
        self.assertEqual(agent.model, "remote-only")

    def test_public_constructor_default_remains_default_state_path(self) -> None:
        default = inspect.signature(Enkiball).parameters["state_path"].default
        self.assertEqual(default, DEFAULT_STATE_PATH)

    def test_missing_config_is_created_with_default_contents(self) -> None:
        config_path = self.root / "new" / "enki.json"

        self.assertEqual(ensure_config(config_path), config_path)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), DEFAULT_CONFIG)

        state_path = self.root / "new" / "state.json"
        config = load_config(config_path, state_path)
        self.assertEqual(config["providers"]["openai"]["models"], {
            "gpt-4o": {"temperature": 0.2, "max_tokens": 4096}
        })
        self.assertEqual(load_state(state_path), {
            "active_provider": "openai", "active_model": "gpt-4o"
        })

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
        self.assertEqual(first._engine._auto_compact_effective_threshold_tokens(), 1400)
        self.assertEqual(second._engine._auto_compact_effective_threshold_tokens(), 700)

    def test_command_handler_uses_agent_state_path(self) -> None:
        state_path = self.root / "cli-agent" / "state.json"
        agent = self.build_agent(state_path)
        cli = EnkiballCLI(agent)

        cli.handler._model(["/model", "command-model"])

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["active_model"], "command-model")
        self.assertEqual(agent.state_path, state_path)

    def test_shared_files_are_read_once_per_agent(self) -> None:
        state_path = self.root / "shared-state.json"
        save_state({"active_provider": "first", "active_model": "one"}, state_path)
        with patch("enkiball.agent.load_state", wraps=load_state) as read_state:
            first = self.build_agent(state_path)
            second = self.build_agent(state_path)
            self.assertEqual(read_state.call_count, 2)
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)

        with patch("enkiball.config.load_state", side_effect=AssertionError("runtime read")), patch(
            "enkiball.config.load_config", side_effect=AssertionError("runtime read")
        ):
            EnkiballCLI(first).handler._model(["/model", "changed"])
            self.assertEqual(second.model, "one")
            first.switch_provider("second")
            second.switch_model("local")
            self.assertEqual((first.provider, first.model), ("second", "two"))
            self.assertEqual((second.provider, second.model), ("first", "local"))
            self.assertEqual(first._engine._build_compact_request(None)["max_tokens"], 200)
            self.assertEqual(second.list_models(remote=False), ["one"])

        save_config({"providers": {}}, self.config_path)
        self.assertEqual(second.list_models(remote=False), ["one"])
        self.assertEqual(first.list_models(remote=False), ["two"])
        self.assertEqual(json.loads(state_path.read_text())["active_provider"], "first")

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
