from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball.cli import _parse_args
from enkiball.llm import ChatEngine
from enkiball.llm_tools import dispatch_tool
from enkiball.sandbox import (
    MAX_OUTPUT_BYTES,
    PermissionDeniedError,
    Sandbox,
    SandboxUnavailableError,
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


class CliPermissionTests(unittest.TestCase):
    def test_cli_defaults_to_interactive_approval(self) -> None:
        args = _parse_args(["--cwd", "."])

        self.assertFalse(args.no_approval)
        self.assertEqual(args.dir_permission, [])

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


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
class ExecSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.external.cleanup)
        self.sandbox = Sandbox(cwd=self.workspace.name)

    def test_command_runs_in_cwd_and_can_write_there(self) -> None:
        code, stdout, stderr = self.sandbox.run("pwd; printf content > output.txt")

        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), self.workspace.name)
        self.assertEqual(stderr, "")
        self.assertEqual(
            (Path(self.workspace.name) / "output.txt").read_text(encoding="utf-8"),
            "content",
        )

    def test_command_cannot_read_unconfigured_external_path(self) -> None:
        secret = Path(self.external.name) / "secret.txt"
        secret.write_text("secret", encoding="utf-8")

        code, stdout, _ = self.sandbox.run(f"cat {secret} 2>/dev/null")

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")

    def test_command_can_use_configured_external_directory(self) -> None:
        secret = Path(self.external.name) / "data.txt"
        secret.write_text("visible", encoding="utf-8")
        sandbox = Sandbox(self.workspace.name, [self.external.name])

        code, stdout, _ = sandbox.run(f"cat {secret}")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "visible")

    def test_network_is_unavailable(self) -> None:
        code, _, _ = self.sandbox.run(
            "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 80), 1)'"
        )

        self.assertNotEqual(code, 0)

    def test_sensitive_host_environment_is_not_inherited(self) -> None:
        with patch.dict(os.environ, {"ENKIBALL_TEST_SECRET": "not-for-child"}):
            code, stdout, _ = self.sandbox.run(
                "printf %s \"${ENKIBALL_TEST_SECRET-unset}\""
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "unset")

    def test_output_is_bounded(self) -> None:
        _, stdout, _ = self.sandbox.run(
            f"python3 -c 'import sys; sys.stdout.write(\"x\" * {MAX_OUTPUT_BYTES + 100})'"
        )

        self.assertLessEqual(len(stdout), MAX_OUTPUT_BYTES + len("\n...[truncated]"))
        self.assertTrue(stdout.endswith("...[truncated]"))

    def test_timeout_raises(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sandbox.run("sleep 5", timeout_sec=1)

    def test_missing_backend_fails_closed(self) -> None:
        with patch("enkiball.sandbox.shutil.which", return_value=None):
            with self.assertRaises(SandboxUnavailableError):
                self.sandbox.run("true")


if __name__ == "__main__":
    unittest.main()
