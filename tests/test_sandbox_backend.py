"""Protocol and authorization tests without system privileges."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
from enkisandbox_runtime.backend import Backend, Server, valid_id
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
                self.backend.request(os.getuid(), os.getgid(), request)
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
