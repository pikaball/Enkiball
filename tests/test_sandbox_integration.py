"""Real runtime checks; point ENKISANDBOX_TEST_SOCKET at a delegated backend."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from enkiball import Enkiball, EnkiSandbox
from enkiball.llm_tools import dispatch_tool
from enkiball.sandbox import TurnInterrupted

SOCKET = os.environ.get("ENKISANDBOX_TEST_SOCKET")
CLIENT = os.environ.get("ENKISANDBOX_CLIENT")


@unittest.skipUnless(
    SOCKET and CLIENT,
    "set ENKISANDBOX_TEST_SOCKET and ENKISANDBOX_CLIENT for real runtime tests",
)
class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sandboxes = []
        self.agents = []
        self.addCleanup(self.cleanup_runtime)

    def cleanup_runtime(self):
        for agent in self.agents:
            agent.shutdown()
        for sandbox in self.sandboxes:
            sandbox.close()

    def sandbox(self, **kwargs):
        kwargs.setdefault("filesystem", [{"path": str(self.root), "access": "write"}])
        obj = EnkiSandbox(client=CLIENT, socket_path=SOCKET, **kwargs)
        self.sandboxes.append(obj)
        return obj

    def agent(self, sandbox):
        obj = Enkiball(sandbox=sandbox, cwd=self.root, persist=False, skill_dirs=[])
        self.agents.append(obj)
        return obj

    def test_file_tools_and_commands_share_isolation(self):
        sandbox = self.sandbox()
        agent = self.agent(sandbox)
        self.assertEqual(
            sandbox.run("id -u", cwd=self.root)[1].strip(), str(os.getuid())
        )
        for name, args in [
            ("write_file", {"path": "file", "content": "before"}),
            ("edit", {"path": "file", "old_text": "before", "new_text": "after"}),
            ("read_file", {"path": "file"}),
        ]:
            result = json.loads(dispatch_tool(name, args, agent._sandbox))
            self.assertTrue(result["ok"], result)
        self.assertEqual(sandbox.run("cat file", cwd=self.root), (0, "after", ""))
        with tempfile.TemporaryDirectory(dir=Path.home()) as external:
            path = Path(external) / "secret"
            path.write_text("secret")
            for tool, args in [
                ("read_file", {"path": str(path)}),
                ("write_file", {"path": str(path), "content": "changed"}),
            ]:
                result = json.loads(dispatch_tool(tool, args, agent._sandbox))
                self.assertFalse(result["ok"], result)
            self.assertNotEqual(
                sandbox.run("cat " + shlex.quote(str(path)), cwd=self.root)[0], 0
            )
            self.assertEqual(path.read_text(), "secret")

    def test_exact_file_write_and_edit_preserve_ungranted_siblings(self):
        target = self.root / "editable"
        target.write_text("before")
        secret = self.root / "private"
        secret.write_text("secret")
        sandbox = self.sandbox(filesystem=[{"path": str(target), "access": "write"}])
        agent = self.agent(sandbox)
        result = json.loads(
            dispatch_tool(
                "edit",
                {"path": str(target), "old_text": "before", "new_text": "after"},
                agent._sandbox,
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.read_text(), "after")
        self.assertNotEqual(sandbox.run("cat private", cwd=self.root)[0], 0)
        self.assertEqual(secret.read_text(), "secret")

    def test_nested_denial_and_read_exception(self):
        secret = self.root / "secret"
        secret.mkdir()
        (secret / "private").write_text("private")
        (secret / "public").write_text("public")
        sandbox = self.sandbox(
            filesystem=[
                {"path": str(self.root), "access": "write"},
                {"path": str(secret), "access": "deny"},
                {"path": str(secret / "public"), "access": "read"},
            ]
        )
        agent = self.agent(sandbox)
        self.assertEqual(
            sandbox.run("cat secret/public", cwd=self.root), (0, "public", "")
        )
        for command in (
            "cat secret/private",
            "ls secret",
            "echo changed > secret/public",
        ):
            self.assertNotEqual(sandbox.run(command, cwd=self.root)[0], 0)
        self.assertFalse(
            json.loads(
                dispatch_tool("read_file", {"path": "secret/private"}, agent._sandbox)
            )["ok"]
        )
        self.assertFalse(
            json.loads(
                dispatch_tool(
                    "write_file",
                    {"path": "secret/public", "content": "changed"},
                    agent._sandbox,
                )
            )["ok"]
        )
        self.assertEqual((secret / "public").read_text(), "public")

    def test_reserved_host_control_paths_are_rejected(self):
        for path in ("/proc/self", "/sys/fs/cgroup", "/dev/shm"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "Reserved"),
            ):
                self.sandbox(filesystem=[{"path": path, "access": "write"}])

    def test_invalid_seccomp_releases_partial_instance(self):
        with self.assertRaisesRegex(ValueError, "Unknown syscall"):
            self.sandbox(
                seccomp={"default": "allow", "deny": ["definitely_not_a_syscall"]}
            )
        self.assertEqual(self.sandbox().run("printf ready", cwd=self.root)[1], "ready")

    def test_destroy_terminates_running_tasks(self):
        sandbox = self.sandbox()
        group = Path(sandbox.inspect()["resources"]["cgroup"])
        sandbox._rpc(
            "exec",
            sandbox_id=sandbox.sandbox_id,
            command="sleep 30",
            cwd=str(self.root),
            env={},
            timeout_sec=60,
        )
        sandbox.destroy()
        self.assertFalse(group.exists())

    def test_protected_hard_links_rejected(self):
        target = self.root / "private"
        target.write_text("private")
        (self.root / "alias").hardlink_to(target)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            self.sandbox(
                filesystem=[
                    {"path": str(self.root), "access": "write"},
                    {"path": str(target), "access": "deny"},
                ]
            )

    def test_symlink_escape_denied(self):
        with tempfile.TemporaryDirectory() as external:
            secret = Path(external) / "secret"
            secret.write_text("secret")
            (self.root / "alias").symlink_to(secret)
            agent = self.agent(self.sandbox())
            self.assertFalse(
                json.loads(
                    dispatch_tool("read_file", {"path": "alias"}, agent._sandbox)
                )["ok"]
            )

    def test_default_policy_does_not_grant_cwd(self):
        sandbox = self.sandbox(filesystem=[])
        self.assertNotEqual(sandbox.run("pwd", cwd=self.root)[0], 0)

    def test_missing_path_creation_requires_type(self):
        path = self.root / "future"
        sandbox = self.sandbox(
            filesystem=[{"path": str(path), "access": "write", "type": "directory"}]
        )
        self.assertEqual(sandbox.run("printf created > file", cwd=path)[0], 0)
        self.assertEqual((path / "file").read_text(), "created")
        with self.assertRaisesRegex(ValueError, "explicit type"):
            self.sandbox(
                filesystem=[{"path": str(self.root / "missing"), "access": "write"}]
            )

    def test_shared_temporary_directory_and_environment_per_task(self):
        sandbox = self.sandbox()
        self.assertEqual(
            sandbox.run(
                'printf shared > /tmp/shared; printf %s "$TASK"',
                cwd=self.root,
                env={"TASK": "one"},
            )[1],
            "one",
        )
        self.assertEqual(
            sandbox.run('cat /tmp/shared; printf %s "${TASK-unset}"', cwd=self.root)[1],
            "sharedunset",
        )
        self.assertEqual(
            self.sandbox().run("test ! -e /tmp/shared", cwd=self.root)[0], 0
        )

    def test_cross_process_attach_and_agent_shutdown(self):
        sandbox = self.sandbox()
        self.agent(sandbox).shutdown()
        source = (
            "from enkiball import EnkiSandbox; "
            f"s=EnkiSandbox.attach({sandbox.sandbox_id!r},client={CLIENT!r},socket_path={SOCKET!r}); "
            f'print(s.run("printf attached",cwd={str(self.root)!r})[1]); s.close()'
        )
        child = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "attached\n")
        self.assertEqual(sandbox.inspect()["state"], "active")

    def test_switch_while_old_command_is_running(self):
        first, second = self.sandbox(), self.sandbox()
        agent = self.agent(first)
        adapter = agent._sandbox
        with concurrent.futures.ThreadPoolExecutor() as pool:
            old = pool.submit(adapter.run, "sleep .3; printf old")
            agent.sandbox = second
            self.assertEqual(agent._sandbox.run("printf new")[1], "new")
            self.assertEqual(old.result()[1], "old")
        self.assertEqual(first.inspect()["state"], "active")

    def test_cancel_one_task_preserves_other_task_and_reaps_descendants(self):
        sandbox = self.sandbox()
        first, second = self.agent(sandbox), self.agent(sandbox)
        cancel = threading.Event()
        command = "python3 -c " + shlex.quote(
            'import os,time; os.setsid(); open("pid", "w").write(str(os.getpid())); time.sleep(30)'
        )
        with concurrent.futures.ThreadPoolExecutor() as pool:
            one = pool.submit(first._sandbox.run, command, cancel_event=cancel)
            two = pool.submit(second._sandbox.run, "sleep .5; printf survived")
            deadline = time.monotonic() + 5
            while not (self.root / "pid").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "pid").exists())
            cancel.set()
            with self.assertRaises(TurnInterrupted):
                one.result(timeout=5)
            self.assertEqual(two.result(timeout=5), (0, "survived", ""))
        info = sandbox.inspect()
        self.assertFalse(any(Path(info["resources"]["cgroup"]).glob("task-*")))

    def test_timeout_and_destroy_cleanup(self):
        sandbox = self.sandbox()
        with self.assertRaises(subprocess.TimeoutExpired):
            sandbox.run("sleep 30", cwd=self.root, timeout_sec=1)
        group = Path(sandbox.inspect()["resources"]["cgroup"])
        sandbox.destroy()
        self.assertFalse(group.exists())

    def test_shared_memory_budget_excludes_harness(self):
        sandbox = self.sandbox(resources={"memory_bytes": 128 * 1024**2})
        first, second = self.agent(sandbox), self.agent(sandbox)
        barrier = threading.Barrier(2)
        command = "python3 -c " + shlex.quote(
            "import time; x=bytearray(80*1024**2); time.sleep(1.5)"
        )

        def run(agent):
            barrier.wait()
            return agent._sandbox.run(command)

        with concurrent.futures.ThreadPoolExecutor() as pool:
            a, b = pool.submit(run, first), pool.submit(run, second)
            results = [a.result(timeout=10), b.result(timeout=10)]
        self.assertTrue(any(result[0] != 0 for result in results), results)
        info = sandbox.inspect()
        events = dict(
            line.split() for line in info["resources"]["memory.events"].splitlines()
        )
        self.assertGreater(int(events["oom_group_kill"]), 0)
        self.assertTrue(any(t["oom_killed"] for t in info["executions"]))
        self.assertNotIn(sandbox.sandbox_id, Path("/proc/self/cgroup").read_text())
        self.assertEqual(
            sandbox.run("printf still-alive", cwd=self.root)[1], "still-alive"
        )

    def test_seccomp_and_bounded_output(self):
        sandbox = self.sandbox(seccomp={"default": "allow", "deny": ["getppid"]})
        self.assertEqual(
            sandbox.run(
                "python3 -c 'import ctypes; print(ctypes.CDLL(None).getppid())'",
                cwd=self.root,
            ),
            (0, "-1\n", ""),
        )
        result = sandbox.run("python3 -c 'print(\"x\" * 1000100)'", cwd=self.root)
        self.assertTrue(result[1].endswith("...[truncated]"))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            futures = [
                pool.submit(
                    sandbox.run,
                    "python3 -c 'import ctypes; print(ctypes.CDLL(None).getppid())'",
                    cwd=self.root,
                )
                for _ in range(4)
            ]
            self.assertEqual([f.result() for f in futures], [(0, "-1\n", "")] * 4)

    def test_image_larger_than_command_output_limit(self):
        import base64

        from enkiball.llm_tools import ImageToolResult

        content = b"\x89PNG\r\n\x1a\n" + b"x" * 1100000
        (self.root / "image.png").write_bytes(content)
        result = dispatch_tool(
            "read_image", {"path": "image.png"}, self.agent(self.sandbox())._sandbox
        )
        self.assertIsInstance(result, ImageToolResult)
        self.assertEqual(base64.b64decode(result.image_url.split(",")[1]), content)

    @unittest.skipUnless(
        os.environ.get("ENKISANDBOX_TEST_CPUSET"), "cpuset delegation required"
    )
    def test_cpu_binding(self):
        sandbox = self.sandbox(resources={"cpu_count": 1})
        self.assertEqual(
            sandbox.run(
                "python3 -c 'import os; print(len(os.sched_getaffinity(0)))'",
                cwd=self.root,
            )[1],
            "1\n",
        )
        self.assertEqual(len(sandbox.inspect()["resources"]["cpus"]), 1)

    @unittest.skipUnless(
        os.environ.get("ENKISANDBOX_TEST_BPF"), "host BPF privileges required"
    )
    def test_paired_loopback_network_and_device_direction(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            sandbox = self.sandbox(
                network={
                    "outbound": [{"interface": "lo", "destinations": ["127.0.0.1/32"]}]
                },
                devices=[{"path": "/dev/null", "access": "read"}],
            )
            self.assertEqual(
                sandbox.run(
                    f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), .5)\"",
                    cwd=self.root,
                )[0],
                0,
            )
            self.assertEqual(sandbox.run("cat /dev/null", cwd=self.root)[0], 0)
            self.assertNotEqual(sandbox.run("echo x > /dev/null", cwd=self.root)[0], 0)
            denied = self.sandbox(
                network={
                    "outbound": [{"interface": "lo", "destinations": ["192.0.2.0/24"]}]
                }
            )
            self.assertNotEqual(
                denied.run(
                    f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), .5)\"",
                    cwd=self.root,
                )[0],
                0,
            )
