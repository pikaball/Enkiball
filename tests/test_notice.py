from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from enkiball.agent import Enkiball
from enkiball.commands import CommandHandler
from enkiball.config import save_config


class SysNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "enki.json"
        save_config({}, self.config_path)
        self.agent = Enkiball(
            config_path=self.config_path,
            state_path=self.root / "state.json",
            session_dir=self.root / "sessions",
            cwd=self.root,
            skill_dirs=[],
            auto_start_mcp=False,
        )
        self.addCleanup(self.agent.shutdown)

    def test_tool_result_receives_all_notices_once(self) -> None:
        self.agent.register_tool(
            lambda _args: '{"ok": true}',
            name="notice_test_tool",
            description="Return a test result",
        )
        self.agent.notice_queue.extend(["first update", "second update"])

        result = self.agent._engine._dispatch_tool("notice_test_tool", {})

        self.assertIn('{"ok": true}', result)
        self.assertIn("<sys-notice>", result)
        self.assertIn('["first update", "second update"]', result)
        self.assertEqual(self.agent.notice_queue, [])
        self.assertEqual(
            self.agent._engine._dispatch_tool("notice_test_tool", {}),
            '{"ok": true}',
        )

    def test_agent_and_engine_share_notice_queue(self) -> None:
        self.agent.register_tool(
            lambda _args: "tool result",
            name="shared_queue_test_tool",
            description="Return a test result",
        )
        self.agent.notice_queue.append("shared queue update")

        self.assertIs(self.agent.notice_queue, self.agent._engine.notice_queue)
        result = self.agent._engine._dispatch_tool("shared_queue_test_tool", {})

        self.assertIn("shared queue update", result)
        self.assertEqual(self.agent.notice_queue, [])

    def test_cli_add_and_show_do_not_consume_notice(self) -> None:
        output = io.StringIO()
        handler = CommandHandler(
            Console(file=output, force_terminal=False),
            self.agent,
        )

        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            self.assertTrue(
                handler.handle(
                    '/notice add "background scan finished"',
                    session,
                )
            )
            self.assertTrue(handler.handle("/notice show", session))

        self.assertEqual(self.agent.notice_queue, ["background scan finished"])
        rendered = output.getvalue()
        self.assertIn("queued notices: 1", rendered)
        self.assertIn("background scan finished", rendered)


if __name__ == "__main__":
    unittest.main()
