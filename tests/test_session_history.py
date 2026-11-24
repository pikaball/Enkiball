from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from enkiball.agent import Enkiball
from enkiball.commands import CommandHandler
from enkiball.llm import ChatEngine
from enkiball.mcp import MCPManager
from enkiball.session_store import SessionStore


class SessionHistoryRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SessionStore._from_paths(root / "sessions", root / "exports")
        self.engine = ChatEngine({})
        self.agent = Enkiball.__new__(Enkiball)
        self.agent._engine = self.engine
        self.agent._config = {}
        self.agent._mcp = MCPManager({})
        self.agent._session_store = self.store
        self.agent._config_path = root / "config.json"
        self.agent._state_path = root / "state.json"
        self.handler = CommandHandler(
            Console(file=io.StringIO(), color_system=None),
            self.agent,
        )

    def test_use_restores_messages_and_requests_cli_replay(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "restored request"},
            {"role": "assistant", "content": "restored answer"},
        ]
        record = self.store.create("target", initial_messages=messages)
        self.store.begin_new("current", initial_messages=self.engine.messages)

        self.handler._session_action("use", [record.session_id])

        self.assertTrue(self.handler.pending_history_replay)
        self.assertEqual(self.store.current_session_id, record.session_id)
        self.assertEqual(self.engine.messages, messages)

    def test_list_prints_latest_last_and_preserves_use_indices(self) -> None:
        records = []
        for day, name in enumerate(("oldest", "middle", "latest"), start=1):
            with patch("enkiball.session_store._now_iso", return_value=f"2026-11-{day:02d}"):
                records.append(self.store.create(name))
        output = io.StringIO()
        self.handler.console = Console(file=output, width=240, color_system=None)

        self.handler._session_action("list", [])

        lines = output.getvalue().splitlines()
        self.assertEqual([line.strip().split(".")[0] for line in lines], ["3", "2", "1"])
        for line, record in zip(lines, records):
            self.assertIn(record.session_id, line)
        self.handler._session_action("use", ["1"])
        self.assertEqual(self.store.current_session_id, records[-1].session_id)

    def test_unsaved_current_session_is_printed_at_bottom(self) -> None:
        saved = self.store.create("saved")
        current = self.store.begin_new()
        output = io.StringIO()
        self.handler.console = Console(file=output, width=240, color_system=None)

        self.handler._session_action("list", [])

        lines = output.getvalue().splitlines()
        self.assertIn(saved.session_id, lines[0])
        self.assertIn(current.session_id, lines[-1])
        self.assertIn("(unsaved)", lines[-1])


if __name__ == "__main__":
    unittest.main()
