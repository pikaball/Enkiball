from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from enkiball import ChatResult, Enkiball, EnkiballCLI
from enkiball.cli import _StreamInputMonitor
from enkiball.system_cli.app import main
from enkiball.llm import ChatEngine
from enkiball.sandbox import TurnInterrupted
from enkiball.system_cli.session_title import SessionTitleHook
from test_stream_usage import Stream, chunk


class CliSessionHookTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.agent = Enkiball(
            config={
                "providers": {"test": {
                    "protocol": "chat-completions", "api_key": "",
                    "base_url": "http://unused.test/v1", "models": {"test-model": {}},
                }},
                "active_provider": "test", "active_model": "test-model",
            },
            state_path=self.root / "state.json",
            session_dir=self.root / "sessions", cwd=self.root,
            skill_dirs=[], builtin_tools=False,
        )
        self.addCleanup(self.agent.shutdown)
        self.output = io.StringIO()
        self.console = Console(file=self.output, color_system=None, width=240)

    def run_cli(self, lines, hook=None):
        cli = EnkiballCLI(
            self.agent, console=self.console, history_path=self.root / "history",
            on_turn_end=hook,
        )
        prompt = Mock()
        prompt.prompt.side_effect = [*lines, EOFError()]
        with patch("enkiball.cli.PromptSession", return_value=prompt), patch(
            "enkiball.cli.sys.stdin.isatty", return_value=False
        ):
            cli.run()
        return cli

    def saved(self):
        store = self.agent.session_store
        return store.load(store.current_session_id)

    def fake_chat(self, text, images, **kwargs):
        self.agent._engine.messages.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": "partial answer"},
        ])
        return ChatResult("partial answer")

    def test_plain_cli_does_not_generate_titles(self):
        with patch.object(self.agent, "chat", side_effect=self.fake_chat), patch(
            "enkiball.system_cli.session_title._generate_title"
        ) as generate:
            self.run_cli(["question", "/exit"])
        generate.assert_not_called()
        self.assertEqual(self.saved().name, "untitled")

    def test_importing_library_does_not_load_system_application(self):
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys; from enkiball import EnkiballCLI, CliTurnContext; "
                "assert not any(name.startswith('enkiball.system_cli') "
                "for name in sys.modules)"
            )],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_import_leaves_cli_dependencies_unloaded(self):
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys, httpx, openai; before = set(sys.modules); "
                "from enkiball import Enkiball; "
                "assert not {'enkiball.cli', 'enkiball.commands', 'rich', 'prompt_toolkit', "
                "'markdown_it', 'pylatexenc'} & (sys.modules.keys() - before); "
                "from enkiball import EnkiballCLI, CliTurnContext, CommandContext, CommandHandler"
            )], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_memory_agent_uses_memory_history_unless_a_path_is_requested(self):
        with Enkiball(persist=False, skill_dirs=[]) as agent:
            target = self.root / "nested" / "input.log"
            for path in (None, target):
                cli = EnkiballCLI(agent, console=self.console, history_path=path)
                with patch("enkiball.cli.PromptSession") as factory:
                    factory.return_value.prompt.side_effect = EOFError
                    cli.run()
                history = factory.call_args.kwargs["history"]
                history.append_string("example input")
                if path is None:
                    self.assertIsInstance(history, InMemoryHistory)
                else:
                    self.assertIn("example input", target.read_text())

    def test_system_entry_point_installs_title_hook(self):
        with patch("enkiball.system_cli.app.Enkiball", return_value=self.agent), patch(
            "enkiball.system_cli.app.EnkiballCLI"
        ) as cli, patch("enkiball.system_cli.app.SessionTitleHook") as title_hook:
            main(["--cwd", str(self.root)])
            context = Mock(interrupted=False)
            cli.call_args.kwargs["on_turn_end"](context)
            title_hook.return_value.assert_called_once_with(context)
        self.assertTrue(callable(cli.call_args.kwargs["poll_background"]))
        cli.return_value.run.assert_called_once_with()

    def test_hook_runs_after_save_and_cleanup_for_all_turn_outcomes(self):
        for outcome in (None, "interrupted-result", TurnInterrupted(), KeyboardInterrupt(), ValueError("failed")):
            with self.subTest(outcome=outcome):
                self.agent.reset()
                contexts = []

                def chat(text, images, **kwargs):
                    result = self.fake_chat(text, images, **kwargs)
                    if isinstance(outcome, BaseException):
                        raise outcome
                    result.interrupted = outcome == "interrupted-result"
                    return result

                def hook(context):
                    contexts.append(context)
                    self.assertEqual(self.saved().messages, self.agent.messages)
                    self.assertFalse(context.should_stop())
                    self.assertIsNone(approval.call_args.args[0])

                with patch.object(self.agent, "chat", side_effect=chat), patch.object(
                    self.agent, "_set_approval_handler"
                ) as approval:
                    self.run_cli(["question", "/exit"], hook)
                self.assertEqual(len(contexts), 1)
                self.assertTrue(contexts[0].first_turn)
                self.assertEqual(contexts[0].text, "question")
                self.assertEqual(contexts[0].interrupted, outcome == "interrupted-result" or isinstance(outcome, (TurnInterrupted, KeyboardInterrupt)))
                self.assertIs(contexts[0].error, outcome if isinstance(outcome, BaseException) else None)

    def test_title_request_is_separate_and_runs_only_after_first_turn(self):
        client = Mock()
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        client.create.side_effect = [
            Stream([chunk({"content": "Fix the session ordering"}, usage)]),
            Stream([chunk({"content": '"会话排序修复"'}, usage)]),
            Stream([chunk({"content": "Second answer"}, usage)]),
        ]
        engines = []

        def get_client(engine):
            engines.append(engine)
            return client

        with patch.object(ChatEngine, "_client", autospec=True, side_effect=get_client):
            self.run_cli(["调整会话列表", "follow-up", "/exit"], SessionTitleHook())

        self.assertEqual(self.saved().name, "会话排序修复")
        self.assertEqual(client.create.call_count, 3)
        self.assertIsNot(engines[1], self.agent._engine)
        self.assertEqual(engines[1].model, self.agent.model)
        self.assertEqual(engines[1].state["active_provider"], self.agent.provider)
        title_request = client.create.call_args_list[1].kwargs
        self.assertNotIn("tools", title_request)
        self.assertIn("调整会话列表", title_request["messages"][1]["content"])
        self.assertIn("Fix the session ordering", title_request["messages"][1]["content"])
        self.assertEqual(self.agent.token_usage.total_tokens, 30)
        self.assertEqual(len(self.agent.messages), 5)
        self.assertNotIn("会话排序修复", str(self.agent.messages))

    def test_interrupted_first_turn_still_gets_title_and_keeps_interrupt_state(self):
        engine = self.agent._engine

        class InterruptedStream(Stream):
            def __iter__(self):
                yield chunk({"content": "partial answer"})
                engine.interrupt()
                raise TurnInterrupted

        client = Mock()
        client.create.side_effect = [
            InterruptedStream([]), Stream([chunk({"content": "Interrupted task"})]),
        ]
        with patch.object(ChatEngine, "_client", return_value=client):
            self.run_cli(["question", "/exit"], SessionTitleHook())
        self.assertEqual(self.saved().name, "Interrupted task")
        self.assertTrue(self.agent.interrupted)
        self.assertEqual(self.agent.messages[-1]["content"], "partial answer")

    def test_keyboard_interrupt_still_generates_and_saves_title_before_exit(self):
        def chat(*args, **kwargs):
            self.fake_chat(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(self.agent, "chat", side_effect=chat), patch(
            "enkiball.system_cli.session_title._generate_title", return_value="Saved before exit"
        ) as generate:
            self.run_cli(["question"], SessionTitleHook())
        generate.assert_called_once()
        self.assertEqual(self.saved().name, "Saved before exit")

    def test_escape_and_ctrl_c_monitor_do_not_cancel_title_hook(self):
        for exit_requested in (False, True):
            with self.subTest(exit_requested=exit_requested):
                self.agent.reset()
                turn_monitor = _StreamInputMonitor()
                turn_monitor._stop_event.set()
                if exit_requested:
                    turn_monitor._exit_event.set()
                hook_monitor = _StreamInputMonitor()
                command_monitor = _StreamInputMonitor()

                def generate(context):
                    self.assertTrue(context.interrupted)
                    self.assertFalse(context.should_stop())
                    return "Interrupted task"

                with patch.object(self.agent, "chat", side_effect=self.fake_chat), patch(
                    "enkiball.cli._StreamInputMonitor",
                    side_effect=[turn_monitor, hook_monitor, command_monitor],
                ), patch("enkiball.system_cli.session_title._generate_title", side_effect=generate) as generate_mock:
                    self.run_cli(["question", "/exit"], SessionTitleHook())
                generate_mock.assert_called_once()
                self.assertEqual(self.saved().name, "Interrupted task")

    def test_title_timeout_keeps_persisted_fallback(self):
        with patch.object(self.agent, "chat", side_effect=self.fake_chat), patch(
            "enkiball.system_cli.session_title._TITLE_TIMEOUT", 0
        ), patch.object(ChatEngine, "_client") as client:
            self.run_cli(["first question", "/exit"], SessionTitleHook())
        client.assert_not_called()
        self.assertEqual(self.saved().name, "first question")

    def test_title_failure_keeps_fallback_and_does_not_retry_next_turn(self):
        for failure in (ValueError("unavailable"), TurnInterrupted(), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.agent.reset()
                with patch.object(self.agent, "chat", side_effect=self.fake_chat), patch(
                    "enkiball.system_cli.session_title._generate_title", side_effect=failure
                ) as generate:
                    self.run_cli(["first question", "second question", "/exit"], SessionTitleHook())
                generate.assert_called_once()
                self.assertEqual(self.saved().name, "first question")

    def test_manual_names_are_preserved_and_new_session_gets_its_own_title(self):
        with patch.object(self.agent, "chat", side_effect=self.fake_chat), patch(
            "enkiball.system_cli.session_title._generate_title", return_value="Generated"
        ) as generate:
            self.run_cli([
                "/session rename Manual", "first",
                "/session new Named", "second",
                "/session new", "third", "/exit",
            ], SessionTitleHook())
        generate.assert_called_once()
        self.assertEqual({s.name for s in self.agent.list_sessions()}, {"Manual", "Named", "Generated"})

    def test_hook_exception_does_not_lose_history_or_stop_repl(self):
        hook = Mock(side_effect=ValueError("hook failed"))
        with patch.object(self.agent, "chat", side_effect=self.fake_chat):
            self.run_cli(["first", "second", "/exit"], hook)
        self.assertEqual(hook.call_count, 2)
        self.assertEqual([m["content"] for m in self.saved().messages if m["role"] == "user"], ["first", "second"])
        self.assertIn("turn end hook error: hook failed", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
