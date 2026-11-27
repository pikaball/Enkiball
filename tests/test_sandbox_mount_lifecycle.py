"""Mount sources across executions of a long-lived sandbox."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
from enkisandbox_runtime.isolation import Mounts, base_policy
from enkisandbox_runtime.backend import Instance
from enkisandbox_runtime.policy import validate_policy


@unittest.skipUnless(hasattr(os, "O_PATH") and shutil.which("bwrap"), "Linux O_PATH and bubblewrap required")
class MountLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state" / "instances" / "instance"
        self.state.mkdir(parents=True)
        self.tmp = self.state / "tmp"
        self.tmp.mkdir()
        self.source = self.root / "runtime-file"
        self.source.write_text("old")
        bases = base_policy() + [{"path": str(self.source), "access": "read"}]
        self.base_patch = patch("enkisandbox_runtime.isolation.base_policy", return_value=bases)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)

    def mounts(self, filesystem=None):
        mounts = Mounts(validate_policy({"filesystem": filesystem or []}, self.root),
                        self.state, os.getuid(), os.getgid(), os.getgroups())
        self.addCleanup(mounts.close)
        return mounts

    def replace_source(self, content="new"):
        replacement = self.root / "replacement"
        replacement.write_text(content)
        replacement.replace(self.source)

    def test_replaced_base_file_is_reopened_for_each_execution(self):
        mounts = self.mounts()
        with mounts.execution("true", "/", self.tmp) as (argv, fds):
            index = argv.index(str(self.source))
            old_source = argv[index - 1]
            old_fd = int(old_source.rsplit("/", 1)[1])
            self.assertIn(old_fd, fds)
            self.assertEqual(Path(old_source).read_text(), "old")
            self.replace_source()
            self.assertEqual(Path(old_source).read_text(), "old")
            with mounts.execution("true", "/", self.tmp) as (next_argv, next_fds):
                next_fd = int(next_argv[next_argv.index(str(self.source)) - 1].rsplit("/", 1)[1])
                self.assertIn(next_fd, next_fds)
                self.assertEqual(Path(f"/proc/self/fd/{next_fd}").read_text(), "new")
            with self.assertRaises(OSError):
                os.fstat(next_fd)
        with self.assertRaises(OSError):
            os.fstat(old_fd)

    def test_inspect_reports_the_instances_original_base_paths(self):
        mounts = self.mounts()
        instance = SimpleNamespace(
            policy={"resources": {}}, mounts=mounts, id="a" * 32, state="active",
            interfaces={}, group=Mock(), tasks={},
        )
        with patch("enkisandbox_runtime.isolation.base_policy", return_value=[]):
            snapshot = Instance.snapshot(instance)
        self.assertIn({"path": str(self.source), "access": "read"}, snapshot["base_filesystem"])

    def test_replacement_works_with_real_bubblewrap(self):
        probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
                               capture_output=True)
        if probe.returncode:
            self.skipTest("bubblewrap namespaces unavailable")
        mounts = self.mounts()
        for expected in ("old", "new"):
            with mounts.execution(f"cat {self.source}", "/", self.tmp) as (argv, fds):
                result = subprocess.run(argv, pass_fds=tuple(fds), capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, expected), result.stderr)
            if expected == "old":
                self.replace_source()

    def test_changed_explicit_paths_fail_closed_and_can_recover(self):
        original = self.root / "explicit"
        original.write_text("content")
        mounts = self.mounts([{"path": str(original), "access": "read"}])
        saved = self.root / "saved"
        original.rename(saved)
        original.write_text("replacement")
        with self.assertRaisesRegex(ValueError, "Pinned policy path changed"):
            with mounts.execution("true", "/", self.tmp):
                pass
        original.unlink()
        saved.rename(original)
        with mounts.execution("true", "/", self.tmp):
            pass

    def test_base_source_rejects_symlink_and_type_change_then_recovers(self):
        mounts = self.mounts()
        self.source.unlink()
        self.source.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symlink"):
            with mounts.execution("true", "/", self.tmp):
                pass
        self.source.unlink()
        self.source.mkdir()
        with self.assertRaisesRegex(ValueError, "type changed"):
            with mounts.execution("true", "/", self.tmp):
                pass
        self.source.rmdir()
        self.source.write_text("recovered")
        with mounts.execution("true", "/", self.tmp) as (argv, _):
            self.assertEqual(Path(argv[argv.index(str(self.source)) - 1]).read_text(), "recovered")

    def test_simultaneous_executions_keep_independent_descriptors(self):
        mounts = self.mounts()
        def run(_):
            with mounts.execution("true", "/", self.tmp) as (argv, _):
                return Path(argv[argv.index(str(self.source)) - 1]).read_text()
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(run, range(32))), ["old"] * 32)
