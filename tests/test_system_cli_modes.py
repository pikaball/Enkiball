from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from enkiball import ChatResult, Enkiball
from enkiball.sandbox import TurnInterrupted
from enkiball.session_store import SessionStore
from enkiball.system_cli.app import _parse_args, main


class SystemCliModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.launch = self.root / "launch"
        self.target = self.root / "target"
        self.launch.mkdir()
        self.target.mkdir()
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.launch)
        (self.launch / "config.json").write_text("{}")
        self.output = io.StringIO()
        self.agents = []
        self.startup_cwds = []
        self.shutdowns = []
        self.outcome = None

        def start_patch(*args, **kwargs):
            patcher = patch(*args, **kwargs)
            self.addCleanup(patcher.stop)
            return patcher.start()

        start_patch("enkiball.session_store.SESSION_DIR", self.root / "sessions")
        start_patch("enkiball.session_store.EXPORT_DIR", self.root / "exports")
        self.store = SessionStore(cwd=self.target)
        start_patch("enkiball.system_cli.app.Console", return_value=Console(
            file=self.output, color_system=None, width=240,
        ))
        start_patch("enkiball.mcp.MCPManager.start_enabled", side_effect=(
            lambda: self.startup_cwds.append(Path.cwd())
        ))

        # Keep construction real: cwd must reach the sandbox and session store
        # before MCP startup, rather than merely appearing in CLI kwargs.
        def make_agent(**kwargs):
            agent = Enkiball(**kwargs, state_path=self.root / "state.json", skill_dirs=[])
            self.agents.append(agent)
            self.addCleanup(agent.shutdown)
            shutdown = patch.object(agent, "shutdown", wraps=agent.shutdown)
            self.addCleanup(shutdown.stop)
            self.shutdowns.append(shutdown.start())
            return agent

        self.agent_factory = start_patch("enkiball.system_cli.app.Enkiball", side_effect=make_agent)
        self.chat = start_patch("enkiball.agent.Enkiball.chat", autospec=True, side_effect=self.reply)
        self.prompt_factory = start_patch("enkiball.cli.PromptSession")
        self.prompt_factory.return_value.prompt.side_effect = EOFError
        self.approval_prompt = start_patch("enkiball.cli._prompt_approval")
        self.tty = start_patch("enkiball.cli.sys.stdin.isatty", return_value=False)
        self.title = start_patch("enkiball.system_cli.app.SessionTitleHook")

    def reply(self, agent, text, images, **kwargs):
        agent.load_messages(agent.messages + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "test answer"},
        ])
        kwargs["on_token"]("test answer")
        # Even an explicitly invoked approval callback cannot prompt in exec.
        if not agent._sandbox.approval:
            self.assertEqual(agent._sandbox._approval_handler(self.root, "read"), "deny")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return ChatResult("test answer", interrupted=self.outcome == "interrupted")

    def invoke(self, *args):
        main(["--config", "config.json", *args])

    def saved_session(self):
        store = self.agents[-1].session_store
        return store.load(store.current_session_id)

    def test_exec_is_one_literal_turn_without_terminal_or_title_requests(self):
        prompt = "/exit\n  Treat this as a prompt.  "
        self.invoke("exec", prompt)
        self.chat.assert_called_once()
        self.assertEqual(self.chat.call_args.args[1], prompt)
        self.prompt_factory.assert_not_called()
        self.approval_prompt.assert_not_called()
        self.tty.assert_not_called()
        self.title.return_value.assert_not_called()
        self.shutdowns[0].assert_called_once()
        self.assertFalse(self.agents[0]._sandbox.approval)
        self.assertIsNone(self.agents[0]._sandbox._approval_handler)
        self.assertIn("test answer", self.output.getvalue())
        self.assertNotIn("CLI ready", self.output.getvalue())
        self.assertEqual(self.saved_session().messages[-2]["content"], prompt)
        self.assertEqual(self.saved_session().cwd, str(self.launch))

    def test_exec_failure_and_interrupt_exit_nonzero_and_save_partial_history(self):
        for outcome, code in ((ValueError("failed"), 1), (TurnInterrupted(), 130),
                              (KeyboardInterrupt(), 130), ("interrupted", 130)):
            with self.subTest(outcome=outcome):
                self.outcome = outcome
                with self.assertRaises(SystemExit) as error:
                    self.invoke("exec", "question")
                self.assertEqual(error.exception.code, code)
                self.assertEqual(self.saved_session().messages[-1]["content"], "test answer")
                self.shutdowns[-1].assert_called_once()
        self.prompt_factory.assert_not_called()
        self.tty.assert_not_called()

    def test_resume_uses_saved_cwd_before_mcp_and_replays_history(self):
        (self.root / "shared").mkdir()
        (self.root / "reference").mkdir()
        record = self.store.create("Original title", [
            {"role": "user", "content": "previous request"},
            {"role": "assistant", "content": "previous answer"},
        ])
        self.prompt_factory.return_value.prompt.side_effect = ["follow-up", EOFError()]
        self.invoke("resume", record.session_id, "--cwd", "nonexistent-ignored",
                    "--dir-permission", "../shared", "--ro-permission", "../reference")
        agent = self.agents[0]
        self.assertEqual(Path.cwd(), self.target)
        self.assertEqual(agent.cwd, self.target)
        self.assertEqual(agent.session_store.cwd, self.target)
        self.assertEqual(self.startup_cwds, [self.target])
        self.assertEqual(agent.config_path, self.launch / "config.json")
        self.assertIn(self.target, agent._sandbox.writable_directories)
        self.assertIn(self.root / "shared", agent._sandbox.writable_directories)
        self.assertIn(self.root / "reference", agent._sandbox.read_only_directories)
        self.assertEqual(agent._sandbox.resolve_path("example.txt", "read"), self.target / "example.txt")
        saved = self.saved_session()
        self.assertEqual(saved.session_id, record.session_id)
        self.assertEqual(saved.name, record.name)
        self.assertEqual(saved.cwd, str(self.target))
        self.assertEqual([m["content"] for m in saved.messages if m["role"] == "user"],
                         ["previous request", "follow-up"])
        self.assertEqual(len(list(self.store.base_dir.glob("*.json"))), 1)
        self.assertIn("previous answer", self.output.getvalue())

    def test_resume_and_exec_append_without_replaying_history(self):
        record = self.store.create("Original title", [
            {"role": "user", "content": "previous request"},
            {"role": "assistant", "content": "previous answer"},
        ])
        self.invoke("exec", "next request", "--session", record.session_id)
        self.prompt_factory.assert_not_called()
        self.assertEqual(self.saved_session().session_id, record.session_id)
        self.assertEqual(self.saved_session().cwd, str(self.target))
        self.assertEqual([m["content"] for m in self.saved_session().messages if m["role"] == "user"],
                         ["previous request", "next request"])
        self.assertNotIn("previous answer", self.output.getvalue())

    def test_bad_resume_never_starts_agent_or_falls_back_to_launch_cwd(self):
        for cwd in (None, "relative", str(self.root / "missing"), str(self.launch / "config.json")):
            (self.store.base_dir / "bad.json").write_text(json.dumps({
                "session_id": "bad", "cwd": cwd, "messages": [],
            }))
            with self.subTest(cwd=cwd), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as error:
                    self.invoke("resume", "bad")
                self.assertEqual(error.exception.code, 1)
                self.assertIn("Cannot resume session", stderr.getvalue())
                self.assertEqual(Path.cwd(), self.launch)
        for session_id in ("missing", "../outside"):
            with self.subTest(session_id=session_id), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    self.invoke("resume", session_id)
                self.assertEqual(error.exception.code, 1)
        self.agent_factory.assert_not_called()
        self.prompt_factory.assert_not_called()

    def test_exec_rejects_empty_prompts_and_diagnostic_combinations(self):
        for argv in (["exec"], ["exec", "  "], ["resume"],
                     ["--sandbox-info", "exec", "hello"],
                     ["--sandbox-info", "resume", "session"]):
            with self.subTest(argv=argv), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    _parse_args(argv)
                self.assertEqual(error.exception.code, 2)

    def test_subcommands_accept_common_options_before_and_after(self):
        for argv in (
            ["--cwd", str(self.target), "exec", "hello", "--no-approval"],
            ["--no-approval", "exec", "hello", "--cwd", str(self.target)],
        ):
            with self.subTest(argv=argv):
                args = _parse_args(argv)
                self.assertEqual(args.command, "exec")
                self.assertEqual(args.exec_prompt, "hello")
                self.assertEqual(args.cwd, self.target)
                self.assertTrue(args.no_approval)
                self.assertEqual(args.dir_permission, [])
                self.assertEqual(args.sandbox_network, "none")
        args = _parse_args(["exec", "hello", "--session", "saved"])
        self.assertEqual(args.resume, "saved")
        args = _parse_args(["resume", "saved"])
        self.assertIsNone(args.exec_prompt)
        self.assertEqual(args.resume, "saved")
        args = _parse_args(["--dir-permission", "/first", "exec", "hello", "--dir-permission", "/second"])
        self.assertEqual(args.dir_permission, ["/first", "/second"])
        args = _parse_args(["exec", "--", "--cwd"])
        self.assertEqual(args.exec_prompt, "--cwd")
        args = _parse_args(["search", "--", "--global"])
        self.assertEqual(args.keyword, "--global")
        self.assertFalse(args.global_search)

    def test_action_flags_are_replaced_by_subcommands(self):
        for argv in (["--exec", "hello"], ["--resume", "saved"],
                     ["search"], ["search", "  "]):
            with self.subTest(argv=argv), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    _parse_args(argv)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
