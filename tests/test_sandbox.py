from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from enkiball.cli import (
    _build_approval_key_bindings,
    _parse_args,
    _prompt_approval,
    main,
)
from enkiball.llm import ChatEngine
from enkiball.llm_tools import dispatch_tool
from enkiball.sandbox import (
    MAX_OUTPUT_BYTES,
    EnkiSandbox,
    PermissionDeniedError,
    Sandbox,
    SandboxUnavailableError,
    TurnInterrupted,
)


class DirectoryPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.external.cleanup)

    def test_cwd_and_relative_paths_are_allowed(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name)

        target = sandbox.resolve_path("nested/file.txt", "write")

        self.assertEqual(target, Path(self.workspace.name) / "nested/file.txt")

    def test_reconfigure_changes_access_and_execution_context(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name)
        target = Path(self.external.name) / "file.txt"
        sandbox.configure(dir_permissions=[self.external.name], network="host", enabled=False)
        self.assertEqual(sandbox.resolve_path(target, "write"), target)
        self.assertEqual(sandbox.command_context["execution_mode"], "host")
        sandbox.configure(dir_permissions=[], enabled=True, network="none")
        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(target, "read")
        self.assertEqual(sandbox.command_context["network"], "host")  # Missing client: actual host mode.

    def test_reconfigure_validation_is_atomic(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name)
        with self.assertRaises(ValueError):
            sandbox.configure(enabled=False, network="invalid")
        self.assertTrue(sandbox.enabled)

    def test_external_path_is_denied_without_approval(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name)
        target = Path(self.external.name) / "file.txt"

        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(target, "read")

    def test_configured_external_directory_is_allowed(self) -> None:
        sandbox = Sandbox(
            cwd=self.workspace.name,
            dir_permissions=[self.external.name],
        )

        target = sandbox.resolve_path(Path(self.external.name) / "file.txt", "read")

        self.assertEqual(target, Path(self.external.name) / "file.txt")

    def test_configured_read_only_directory_rejects_file_writes(self) -> None:
        protected = Path(self.workspace.name) / "protected"
        protected.mkdir()
        target = protected / "file.txt"
        target.write_text("original", encoding="utf-8")
        sandbox = Sandbox(
            cwd=self.workspace.name,
            ro_permissions=[protected],
        )

        self.assertEqual(sandbox.resolve_path(target, "read"), target)
        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(target, "write")

    def test_approval_grants_directory_for_session(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        requests: list[tuple[Path, str]] = []
        sandbox.set_approval_handler(
            lambda path, operation: requests.append((path, operation)) or True
        )
        first = Path(self.external.name) / "first.txt"
        second = Path(self.external.name) / "second.txt"

        sandbox.resolve_path(first, "write")
        sandbox.set_approval_handler(lambda path, operation: False)
        sandbox.resolve_path(second, "read")

        self.assertEqual(requests, [(Path(self.external.name), "write")])

    def test_approve_once_does_not_grant_directory_for_session(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        sandbox.set_approval_handler(lambda path, operation: "once")
        first = Path(self.external.name) / "first.txt"

        sandbox.resolve_path(first, "read")
        sandbox.set_approval_handler(lambda path, operation: "deny")

        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(Path(self.external.name) / "second.txt", "read")

    def test_read_approval_does_not_grant_write_access(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        sandbox.set_approval_handler(lambda path, operation: "always")
        target = Path(self.external.name) / "file.txt"

        sandbox.resolve_path(target, "read")
        sandbox.set_approval_handler(lambda path, operation: "deny")

        self.assertIn(Path(self.external.name), sandbox.read_only_directories)
        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(target, "write")

    def test_write_approval_also_grants_read_access(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        sandbox.set_approval_handler(lambda path, operation: "always")
        target = Path(self.external.name) / "file.txt"

        sandbox.resolve_path(target, "write")
        sandbox.set_approval_handler(lambda path, operation: "deny")

        self.assertEqual(sandbox.resolve_path(target, "read"), target)

    def test_approval_without_frontend_fails_closed(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)

        with self.assertRaises(PermissionDeniedError):
            sandbox.resolve_path(Path(self.external.name) / "file.txt", "read")

    def test_dispatch_reports_permission_denial(self) -> None:
        path = Path(self.external.name) / "file.txt"
        path.write_text("secret", encoding="utf-8")

        result = json.loads(
            dispatch_tool("read_file", {"path": str(path)}, Sandbox(self.workspace.name))
        )

        self.assertFalse(result["ok"])
        self.assertIn("outside allowed directories", result["error"])

    def test_engine_uses_its_configured_sandbox(self) -> None:
        path = Path(self.external.name) / "file.txt"
        path.write_text("visible", encoding="utf-8")
        engine = ChatEngine(
            {},
            sandbox=Sandbox(self.workspace.name, [self.external.name]),
        )

        result = json.loads(engine._dispatch_tool("read_file", {"path": str(path)}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "visible")


class CommandContextTests(unittest.TestCase):
    def setUp(self):
        client = patch('enkiball.sandbox.shutil.which', return_value='/client')
        client.start()
        self.addCleanup(client.stop)
        def rpc(op, **data):
            if op == 'create':
                return {'execution_mode': 'sandbox', 'sandbox_id': 'a' * 32, 'effective_policy': data['policy']}
            return {'state': 'destroyed'}
        backend = patch.object(EnkiSandbox, '_rpc', side_effect=rpc)
        backend.start()
        self.addCleanup(backend.stop)

    @staticmethod
    def description(engine):
        return next(
            schema["function"]["description"] for schema in engine._get_tools()
            if schema["function"]["name"] == "run_command"
        )

    def test_schema_tracks_permissions_and_approval_without_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as external:
            sandbox = Sandbox(cwd=workspace, approval=True)
            engine = ChatEngine({}, state={}, sandbox=sandbox)
            initial = self.description(engine)
            self.assertIn('"execution_mode": "sandbox"', initial)
            self.assertIn('"network": {"outbound": []}', initial)
            self.assertIn('"privileged_approval_available": false', initial)
            sandbox.set_approval_handler(lambda path, operation: "always")
            sandbox.resolve_path(Path(external) / "file.txt", "read")
            updated = self.description(engine)
            self.assertIn('"privileged_approval_available": true', updated)
            self.assertIn(external, updated)
            self.assertNotIn(external, initial)
            sandbox.set_approval_handler(None)
            self.assertIn('"privileged_approval_available": false', self.description(engine))
            from enkiball.llm_tools import TOOL_SCHEMAS
            self.assertNotIn("Current execution context", str(TOOL_SCHEMAS))

    def test_host_and_shared_network_are_reported_accurately(self):
        with tempfile.TemporaryDirectory() as workspace:
            sandbox = Sandbox(cwd=workspace, network="host", env={"SECRET": "private-value"})
            engine = ChatEngine({}, state={}, sandbox=sandbox)
            description = self.description(engine)
            self.assertIn('"network": "all"', description)
            self.assertIn('"execution_mode": "sandbox"', description)
            self.assertNotIn("private-value", description)
            sandbox.enabled = False
            description = self.description(engine)
            self.assertIn('"execution_mode": "host"', description)
            self.assertIn("privilleged=true is unnecessary", description)
            self.assertNotIn("will be denied", description)


class CliPermissionTests(unittest.TestCase):
    def _approval_from_input(self, data: str) -> str:
        with create_pipe_input() as pipe_input:
            session: PromptSession[str] = PromptSession(
                multiline=False,
                key_bindings=_build_approval_key_bindings(),
                input=pipe_input,
                output=DummyOutput(),
            )
            pipe_input.send_text(data)
            return _prompt_approval(session)

    def test_cli_defaults_to_interactive_approval(self) -> None:
        args = _parse_args(["--cwd", "."])

        self.assertFalse(args.no_approval)
        self.assertFalse(args.privilleged)
        self.assertEqual(args.dir_permission, [])
        self.assertEqual(args.ro_permission, [])
        self.assertEqual(args.sandbox_network, "none")
        self.assertEqual(args.sandbox_env, [])

    def test_cli_accepts_sandbox_network_and_environment(self) -> None:
        args = _parse_args(
            ["--sandbox-network", "host", "--sandbox-env", "HTTP_PROXY"]
        )

        self.assertEqual(args.sandbox_network, "host")
        self.assertEqual(args.sandbox_env, ["HTTP_PROXY"])

    def test_cli_accepts_repeated_directory_permissions(self) -> None:
        args = _parse_args(
            [
                "--dir-permission",
                "/first",
                "--dir-permission",
                "/second",
                "--no-approval",
            ]
        )

        self.assertEqual(args.dir_permission, ["/first", "/second"])
        self.assertTrue(args.no_approval)

    def test_cli_accepts_repeated_read_only_permissions(self) -> None:
        args = _parse_args(
            ["--ro-permission", "/first", "--ro-permission", "/second"]
        )

        self.assertEqual(args.ro_permission, ["/first", "/second"])

    def test_cli_accepts_privilleged_mode(self) -> None:
        args = _parse_args(["--privilleged"])

        self.assertTrue(args.privilleged)

    def test_cli_privilleged_mode_disables_sandbox_and_allows_root(self) -> None:
        with patch("enkiball.cli.Enkiball") as agent_class:
            with patch("enkiball.cli.EnkiballCLI") as cli_class:
                main(["--cwd", ".", "--privilleged"])

        kwargs = agent_class.call_args.kwargs
        self.assertFalse(kwargs["sandbox"])
        self.assertEqual(kwargs["dir_permissions"], [Path.cwd().anchor])
        self.assertEqual(kwargs["ro_permissions"], [])
        self.assertEqual(kwargs["sandbox_network"], "none")
        self.assertEqual(kwargs["sandbox_inherit_env"], [])
        cli_class.return_value.run.assert_called_once_with()

    def test_escape_immediately_interrupts_approval(self) -> None:
        with self.assertRaises(TurnInterrupted):
            self._approval_from_input("\x1b")

    def test_approval_numeric_choices(self) -> None:
        self.assertEqual(self._approval_from_input("1\n"), "once")
        self.assertEqual(self._approval_from_input("2\n"), "always")
        self.assertEqual(self._approval_from_input("3\n"), "deny")

    def test_privileged_approval_only_allows_once_or_deny(self) -> None:
        with create_pipe_input() as pipe_input:
            session: PromptSession[str] = PromptSession(
                multiline=False,
                key_bindings=_build_approval_key_bindings(),
                input=pipe_input,
                output=DummyOutput(),
            )
            pipe_input.send_text("1\n")
            self.assertEqual(_prompt_approval(session, privileged=True), "once")

        with create_pipe_input() as pipe_input:
            session = PromptSession(
                multiline=False,
                key_bindings=_build_approval_key_bindings(),
                input=pipe_input,
                output=DummyOutput(),
            )
            pipe_input.send_text("2\n")
            self.assertEqual(_prompt_approval(session, privileged=True), "deny")

    def test_control_c_keeps_prompt_toolkit_default_behavior(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._approval_from_input("\x03")


class HostExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.external.cleanup)

    def test_disabled_sandbox_runs_on_host_without_bubblewrap(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, sandbox=False)
        target = Path(self.external.name) / "host.txt"

        with patch.dict(os.environ, {"ENKIBALL_HOST_TEST": "inherited"}):
            with patch(
                "enkiball.sandbox.EnkiSandbox._rpc",
                side_effect=AssertionError("bubblewrap must not be used"),
            ):
                code, stdout, stderr = sandbox.run(
                    f"printf %s \"$ENKIBALL_HOST_TEST\"; printf host > {target}"
                )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "inherited")
        self.assertEqual(stderr, "")
        self.assertEqual(target.read_text(encoding="utf-8"), "host")

    def test_privileged_command_requires_one_time_approval(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        decisions = iter(("once", "deny"))
        requests: list[tuple[Path, str]] = []
        sandbox.set_approval_handler(
            lambda path, operation: requests.append((path, operation))
            or next(decisions)
        )

        code, stdout, _ = sandbox.run("printf first", privileged=True)
        with self.assertRaises(PermissionDeniedError):
            sandbox.run("printf second", privileged=True)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "first")
        self.assertEqual(
            requests,
            [
                (Path(self.workspace.name), "privileged command"),
                (Path(self.workspace.name), "privileged command"),
            ],
        )

    def test_privileged_command_without_frontend_is_denied(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)

        with self.assertRaises(PermissionDeniedError):
            sandbox.run("true", privileged=True)

    def test_privileged_command_does_not_accept_always(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, approval=True)
        sandbox.set_approval_handler(lambda path, operation: "always")

        with self.assertRaises(PermissionDeniedError):
            sandbox.run("true", privileged=True)

    def test_dispatch_passes_privileged_flag(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name)
        with patch.object(
            sandbox,
            "run",
            return_value=(0, "ok", ""),
        ) as run:
            result = json.loads(
                dispatch_tool(
                    "run_command",
                    {"command": "true", "privilleged": True},
                    sandbox,
                )
            )

        self.assertTrue(result["ok"])
        run.assert_called_once_with(
            command="true",
            timeout_sec=120,
            privileged=True,
            cancel_event=None,
        )

    def test_cancel_event_kills_running_command(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, sandbox=False)
        cancel_event = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                sandbox.run("sleep 30", cancel_event=cancel_event)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)
        cancel_event.set()
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TurnInterrupted)

    def test_dispatch_rejects_implicit_argument_coercion(self) -> None:
        sandbox = Sandbox(cwd=self.workspace.name, sandbox=False)
        cases = (
            ("read_file", {"path": None}, "path must be a string"),
            (
                "read_file",
                {"path": "x", "start": "0"},
                "start must be an integer",
            ),
            (
                "read_file",
                {"path": "x", "end": False},
                "end must be an integer",
            ),
            (
                "write_file",
                {"path": "x", "content": None},
                "content must be a string",
            ),
            (
                "write_file",
                {"path": "x", "content": "x", "append": "false"},
                "append must be a boolean",
            ),
            (
                "run_command",
                {"command": "true", "timeout_sec": "1"},
                "timeout_sec must be an integer",
            ),
        )

        for name, args, error in cases:
            with self.subTest(name=name, args=args):
                result = json.loads(dispatch_tool(name, args, sandbox))
                self.assertFalse(result["ok"])
                self.assertIn(error, result["error"])


if __name__ == "__main__":
    unittest.main()
