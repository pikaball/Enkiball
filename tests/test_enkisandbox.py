from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball import (
    Enkiball,
    EnkiSandbox,
    PermissionDeniedError,
    SandboxUnavailableError,
)
from enkiball.llm_tools import dispatch_tool
from enkiball.sandbox import TurnInterrupted


class SandboxClientTests(unittest.TestCase):
    def fake_rpc(self, operation, **data):
        if operation == "create" or operation == "inspect":
            return {
                "sandbox_id": "a" * 32,
                "execution_mode": "sandbox",
                "effective_policy": data.get(
                    "policy", {"filesystem": [], "resources": {}}
                ),
            }
        if operation == "exec":
            return {"execution_id": data["execution_id"], "state": "running"}
        if operation == "wait":
            return {
                "execution_id": data["execution_id"],
                "state": "exited",
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
            }
        return {"state": "destroyed"}

    def test_missing_client_reports_host_and_ignores_policy(self):
        with patch("enkiball.sandbox.shutil.which", return_value=None):
            with EnkiSandbox(
                filesystem=[{"path": "/", "access": "deny"}],
                resources={"memory_bytes": 1},
            ) as sandbox:
                self.assertEqual(sandbox.execution_mode, "host")
                self.assertIsNone(sandbox.sandbox_id)
                self.assertEqual(sandbox.effective_policy, {})
                self.assertEqual(sandbox.run("printf host"), (0, "host", ""))
                with Enkiball(sandbox=sandbox, persist=False, skill_dirs=[]) as agent:
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "file"
                        result = json.loads(
                            dispatch_tool(
                                "write_file",
                                {"path": str(path), "content": "host"},
                                agent._sandbox,
                            )
                        )
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(path.read_text(), "host")

    def test_client_connection_failure_does_not_fall_back(self):
        with (
            patch("enkiball.sandbox.shutil.which", return_value="/client"),
            patch(
                "enkiball.sandbox.subprocess.run",
                return_value=subprocess.CompletedProcess([], 69, "", "refused"),
            ),
        ):
            with self.assertRaises(SandboxUnavailableError):
                EnkiSandbox()

    def test_attach_requires_client(self):
        with patch("enkiball.sandbox.shutil.which", return_value=None):
            with self.assertRaises(SandboxUnavailableError):
                EnkiSandbox.attach("a" * 32)

    def test_policy_immutable_and_owner_close(self):
        policy = {"memory_bytes": 1000}
        with (
            patch("enkiball.sandbox.shutil.which", return_value="/client"),
            patch.object(EnkiSandbox, "_rpc", side_effect=self.fake_rpc) as rpc,
        ):
            sandbox = EnkiSandbox(resources=policy)
            policy["memory_bytes"] = 1
            snapshot = sandbox.effective_policy
            snapshot["resources"]["memory_bytes"] = 0
            self.assertEqual(
                sandbox.effective_policy["resources"]["memory_bytes"], 1000
            )
            with self.assertRaises(AttributeError):
                sandbox.effective_policy = {}
            self.assertEqual(sandbox.run("true"), (0, "ok", ""))
            sandbox.close()
            sandbox.close()
            self.assertEqual(
                [c.args[0] for c in rpc.call_args_list].count("destroy"), 1
            )
            with self.assertRaises(RuntimeError):
                sandbox.run("true")

    def test_attached_close_detaches_and_destroy_requires_explicit_flag(self):
        with (
            patch("enkiball.sandbox.shutil.which", return_value="/client"),
            patch.object(EnkiSandbox, "_rpc", side_effect=self.fake_rpc) as rpc,
        ):
            attached = EnkiSandbox.attach("a" * 32)
            with self.assertRaises(PermissionDeniedError):
                attached.destroy()
            attached.close()
            self.assertNotIn("destroy", [c.args[0] for c in rpc.call_args_list])
            other = EnkiSandbox.attach("a" * 32)
            other.destroy(allow_attached=True)
            self.assertEqual(rpc.call_args_list[-1].args[0], "destroy")

    def test_agent_switch_keeps_shared_instance_alive(self):
        with (
            patch("enkiball.sandbox.shutil.which", return_value="/client"),
            patch.object(EnkiSandbox, "_rpc", side_effect=self.fake_rpc) as rpc,
        ):
            first, second = EnkiSandbox(), EnkiSandbox()
            agent = Enkiball(sandbox=first, persist=False, skill_dirs=[])
            old_adapter = agent._engine.sandbox
            agent.sandbox = second
            self.assertIs(agent.sandbox, second)
            self.assertIs(old_adapter.runtime, first)
            with self.assertRaises(ValueError):
                agent.configure_sandbox(enabled=False)
            agent.shutdown()
            self.assertNotIn("destroy", [c.args[0] for c in rpc.call_args_list])
            first.close()
            second.close()

    def test_cancel_preserves_execution_identity(self):
        event = threading.Event()

        def rpc(operation, **data):
            result = self.fake_rpc(operation, **data)
            if operation == "exec":
                event.set()
            return result

        with (
            patch("enkiball.sandbox.shutil.which", return_value="/client"),
            patch.object(EnkiSandbox, "_rpc", side_effect=rpc) as call,
        ):
            sandbox = EnkiSandbox()
            with self.assertRaises(TurnInterrupted):
                sandbox.run("sleep 30", cancel_event=event)
            execution = next(
                c.kwargs["execution_id"]
                for c in call.call_args_list
                if c.args[0] == "exec"
            )
            self.assertEqual(call.call_args.args, ("cancel",))
            self.assertEqual(call.call_args.kwargs["execution_id"], execution)
            sandbox.close()
