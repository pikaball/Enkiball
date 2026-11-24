from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from enkiball.config import load_config, save_config, set_mcp_enabled
from enkiball.mcp import MCPManager


class MCPConfigFormatTests(unittest.TestCase):
    def test_mixed_file_round_trips_in_flat_format(self) -> None:
        config = {
            "providers": {},
            "mcp": {
                "servers": {
                    "local": {"command": "old", "args": ["old-arg"]},
                    "remote": {"url": "https://example.test/mcp"},
                },
                "local": {"command": "new"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enki.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_config(path, state={})
            self.assertEqual(loaded["mcp"], {
                "local": {"command": "new"},
                "remote": {"url": "https://example.test/mcp"},
            })
            self.assertTrue(set_mcp_enabled(loaded, "remote", False))
            manager = MCPManager(loaded)
            self.assertTrue(manager.remove_server("local"))
            manager.add_server("servers", {"command": "node"})
            save_config(loaded, path)
            expected = {
                "remote": {"url": "https://example.test/mcp", "enabled": False},
                "servers": {"command": "node"},
            }
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcp"], expected)
            self.assertEqual(load_config(path, state={})["mcp"], expected)


class MCPEnvironmentConfigTests(unittest.TestCase):
    def test_environment_is_passed_to_stdio_server(self) -> None:
        client = Mock(running=True, tools=[], resources=[], prompts=[])
        config = {
            "mcp": {
                "local": {
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"SHARED": "legacy"},
                    "environment": {"SHARED": "current", "TOKEN": "secret"},
                }
            }
        }

        with patch("enkiball.mcp.StdioMCPClient", return_value=client) as factory:
            ok, message = MCPManager(config).start("local")

        self.assertTrue(ok, message)
        factory.assert_called_once_with(
            "local",
            "node",
            ["server.js"],
            {"SHARED": "current", "TOKEN": "secret"},
            transport="jsonl",
        )
        client.start.assert_called_once_with()

    def test_invalid_environment_is_rejected_before_start(self) -> None:
        config = {
            "mcp": {
                "servers": {
                    "local": {
                        "command": "node",
                        "environment": {"BAD=NAME": "value"},
                    }
                }
            }
        }

        with patch("enkiball.mcp.StdioMCPClient") as factory:
            ok, message = MCPManager(config).start("local")

        self.assertFalse(ok)
        self.assertIn("invalid MCP environment variable name", message)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
