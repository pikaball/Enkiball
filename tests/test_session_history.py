from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
