"""Protocol and authorization tests without system privileges."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
from enkisandbox_runtime.backend import (
    Backend, Instance, Server, configure_socket_permissions, peer_groups, valid_id,
)
from enkisandbox_runtime.isolation import Mounts
from enkisandbox_runtime.policy import validate_policy

CLIENT = Path(__file__).resolve().parents[1] / "sandbox/build/enkisandbox-client"


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cgroup = self.root / "cgroup"
        self.cgroup.mkdir()
        self.backend = Backend(
            self.root / "state",
            self.cgroup,
            {
                "12345": {
                    "read_paths": ["/reference"],
                    "write_paths": ["/work"],
                    "devices": [],
                    "capabilities": [],
                    "resources": {"memory_bytes": 1000},
                    "networks": [
                        {
                            "outbound": [
                                {"interface": "lo", "destinations": ["127.0.0.0/8"]}
                            ]
                        }
                    ],
                }
            },
        )
        self.addCleanup(self.backend.close)

    def test_authorization_applies_to_paths_network_capabilities_and_limits(self):
        def policy(**kwargs):
            return validate_policy(
                {
                    "filesystem": [{"path": "/work", "access": "write"}],
                    "resources": {"memory_bytes": 1000},
                    **kwargs,
                },
                Path("/"),
            )

        self.backend.authorize(12345, policy())
        for invalid in [
            policy(filesystem=[{"path": "/etc", "access": "write"}]),
            policy(resources={}),
            policy(resources={"memory_bytes": 1001}),
            policy(capabilities=["NET_ADMIN"]),
            policy(network="all"),
            policy(devices=[{"path": "/dev/null", "access": "read"}]),
        ]:
            with self.subTest(policy=invalid), self.assertRaises(PermissionError):
                self.backend.authorize(12345, invalid)
        allowed = policy(
            network={"outbound": [{"interface": "lo", "destinations": ["127.0.0.0/8"]}]}
        )
        self.backend.authorize(12345, allowed)
        allowed["network"]["outbound"][0]["interface"] = "eth0"
        with self.assertRaises(PermissionError):
            self.backend.authorize(12345, allowed)

    def test_ids_are_not_authorization(self):
        instance = Mock(uid=12345)
        identity = "a" * 32
        self.backend.instances[identity] = instance
        for op in ("inspect", "exec", "cancel", "destroy", "wait"):
            with self.subTest(op=op), self.assertRaises(PermissionError):
                self.backend.request(
                    12346, 12346, {"version": 1, "op": op, "sandbox_id": identity}
                )
        self.backend.instances.clear()

    def test_missing_authorization_limits_do_not_restrict_policy(self):
        requested = validate_policy(
            {"filesystem": [{"path": "/work", "access": "write"}],
             "capabilities": ["NET_ADMIN"], "network": "all"}, Path("/")
        )
        self.backend.authorize(12346, requested)
        self.backend.authorization["12346"] = {}
        self.backend.authorize(12346, requested)

    def test_socket_peer_groups_match_connecting_process(self):
        first, second = socket.socketpair()
        try:
            self.assertEqual(set(peer_groups(first)), set(os.getgroups()))
        finally:
            first.close()
            second.close()

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "ACL tools required")
    def test_socket_acl_uses_configured_uids(self):
        users = self.root / "socket-users"
        users.write_text("# allowed\n65534\n")
        users.chmod(0o600)
        path = self.root / "acl.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            configure_socket_permissions(path, None, users)
            acl = subprocess.run(["getfacl", "--omit-header", "-n", str(path)],
                                 check=True, capture_output=True, text=True).stdout
            self.assertIn("user:65534:rw-", acl)
            self.assertIn("other::---", acl)
            users.write_text("not-a-uid\n")
            with self.assertRaises(ValueError):
                configure_socket_permissions(path, None, users)

    def test_instance_uses_peer_groups_without_rebuilding_account_groups(self):
        with patch("enkisandbox_runtime.backend.os.geteuid", return_value=0), patch(
            "enkisandbox_runtime.backend.os.chown"
        ), patch("enkisandbox_runtime.backend.ResourceGroup"), patch(
            "enkisandbox_runtime.backend.Mounts"
        ) as mounts:
            instance = Instance(
                self.backend, os.getuid(), os.getgid(), validate_policy({}, self.root), []
            )
            try:
                self.assertEqual(instance.groups, [])
                self.assertEqual(mounts.call_args.args[-1], [])
            finally:
                instance.close()

    def test_private_umask_and_existing_state_allow_directory_traversal(self):
        state = self.root / "private-state"
        state.mkdir(mode=0o700)
        previous = os.umask(0o777)
        try:
            backend = Backend(state, self.cgroup)
            try:
                with patch("enkisandbox_runtime.backend.ResourceGroup"), patch(
                    "enkisandbox_runtime.backend.Mounts"
                ):
                    instance = Instance(
                        backend, os.getuid(), os.getgid(), validate_policy({}, self.root),
                        os.getgroups(),
                    )
                    try:
                        self.assertEqual(state.stat().st_mode & 0o777, 0o711)
                        self.assertEqual(instance.directory.stat().st_mode & 0o777, 0o711)
                        self.assertEqual(
                            (instance.directory / "tmp").stat().st_mode & 0o777, 0o700
                        )
                        self.assertEqual(
                            (instance.directory / "tmp/enkiball-home").stat().st_mode & 0o777,
                            0o700,
                        )
                    finally:
                        instance.close()
            finally:
                backend.close()
        finally:
            os.umask(previous)

    def test_state_created_under_restrictive_umask_is_reusable(self):
        state = self.root / "new-state"
        previous = os.umask(0o777)
        try:
            backend = Backend(state, self.cgroup)
            backend.close()
            self.assertEqual(state.stat().st_mode & 0o777, 0o711)
            self.assertEqual((state / "backend.lock").stat().st_mode & 0o777, 0o600)
            Backend(state, self.cgroup).close()
        finally:
            os.umask(previous)

    def test_protocol_rejects_versions_identity_spoofing_and_relative_paths(self):
        requests = [
            {"version": 2, "op": "create"},
            {"version": True, "op": "create"},
            {"version": 1, "op": "create", "uid": 0, "policy": {}},
            {
                "version": 1,
                "op": "create",
                "policy": {"filesystem": [{"path": ".", "access": "write"}]},
            },
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.backend.request(os.getuid(), os.getgid(), request, os.getgroups())
        for identity in ("../" * 10, "a" * 31, "A" * 32, None):
            with self.assertRaises(ValueError):
                valid_id(identity)

    def test_restart_reaps_old_instance_groups_and_temp_data(self):
        stale = "a" * 32
        group = self.cgroup / ("sandbox-" + stale)
        group.mkdir()
        state = self.root / "other-state"
        (state / stale).mkdir(parents=True)
        (state / stale / "data").write_text("old")
        with patch("enkisandbox_runtime.backend.remove_group") as cleanup:
            other = Backend(state, self.cgroup)
            other.close()
            cleanup.assert_called_once_with(group)
        self.assertFalse((state / stale).exists())

    @unittest.skipUnless(CLIENT.exists(), "make -C sandbox first")
    def test_elf_client_uses_peer_credentials_and_structured_errors(self):
        path = self.root / "control.sock"
        server = Server(path, self.backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(
                self.backend, "request", return_value={"state": "checked"}
            ) as request:
                response = subprocess.run(
                    [str(CLIENT), "--socket", str(path)],
                    input=json.dumps(
                        {"version": 1, "op": "inspect", "sandbox_id": "a" * 32}
                    ),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertTrue(json.loads(response.stdout)["ok"])
                self.assertEqual(request.call_args.args[:2], (os.getuid(), os.getgid()))
            response = subprocess.run(
                [str(CLIENT), "--socket", str(path)],
                input='{"version":99}',
                capture_output=True,
                text=True,
            )
            self.assertEqual(response.returncode, 0)
            self.assertFalse(json.loads(response.stdout)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unsafe_capability_combinations_fail_before_execution(self):
        for caps, network in [
            (["SYS_ADMIN"], {"outbound": []}),
            (["NET_ADMIN"], "all"),
        ]:
            with self.assertRaises(ValueError):
                Mounts(
                    validate_policy(
                        {"capabilities": caps, "network": network}, self.root
                    ),
                    self.root,
                    os.getuid(),
                    os.getgid(),
                    os.getgroups(),
                )
