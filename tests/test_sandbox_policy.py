from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball.sandbox_policy import validate_policy

RUNTIME = Path(__file__).resolve().parents[1] / "sandbox" / "enkisandbox_runtime"


def runtime_module(name):
    spec = importlib.util.spec_from_file_location(
        "runtime_" + name, RUNTIME / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_policy_is_detached_and_paths_are_anchored(self):
        policy = {
            "filesystem": [{"path": "future", "access": "write", "type": "directory"}],
            "resources": {"cpu_count": 2},
        }
        result = validate_policy(policy, self.root)
        self.assertEqual(result["filesystem"][0]["path"], str(self.root / "future"))
        policy["resources"]["cpu_count"] = 0
        self.assertEqual(result["resources"]["cpu_count"], 2)
        self.assertEqual(
            runtime_module("policy").validate_policy(
                policy | {"resources": {}}, self.root
            ),
            validate_policy(policy | {"resources": {}}, self.root),
        )

    def test_invalid_policies(self):
        policies = [
            {"resources": {"cpu_count": True}},
            {"resources": {"memory_bytes": 0}},
            {"resources": {"swap": 0}},
            {"capabilities": ["BAD"]},
            {
                "filesystem": [
                    {"path": ".", "access": "read"},
                    {"path": ".", "access": "write"},
                ]
            },
            {"filesystem": [{"path": ".", "access": "bad"}]},
            {"seccomp": {"default": "allow", "allow": ["open"]}},
            {"network": {"outbound": [{"destinations": ["invalid"]}]}},
            {
                "network": {
                    "outbound": [
                        {"interface": "x;drop", "destinations": ["10.0.0.0/8"]}
                    ]
                }
            },
        ]
        for policy in policies:
            with (
                self.subTest(policy=policy),
                self.assertRaises((TypeError, ValueError)),
            ):
                validate_policy(policy, self.root)

    def test_bpf_pairs_ipv4_ipv6_and_device_directions(self):
        module = runtime_module("bpf")
        with patch("socket.if_nametoindex", side_effect=[7, 1]):
            source = module.policy_source(
                {
                    "outbound": [
                        {"interface": "eth0", "destinations": ["10.0.0.0/8"]},
                        {"interface": "lo", "destinations": ["::1/128"]},
                    ]
                },
                [{"path": "/dev/null", "access": "read"}],
            )
        self.assertIn(
            "version == 4 && s->ifindex == 7 && (address[0] & 255) == 10", source
        )
        self.assertIn("version == 6 && s->ifindex == 1", source)
        self.assertIn("~2", source)
        self.assertIn("return 0; }", source)

    @unittest.skipUnless(shutil.which("clang"), "clang required")
    def test_bpf_program_compiles(self):
        source = runtime_module("bpf").policy_source(
            {"outbound": [{"destinations": ["127.0.0.0/8", "::1/128"]}]},
            [{"path": "/dev/null", "access": "read"}],
        )
        src, obj = self.root / "filter.c", self.root / "filter.o"
        src.write_text(source)
        result = subprocess.run(
            ["clang", "-O2", "-target", "bpf", "-c", str(src), "-o", str(obj)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
