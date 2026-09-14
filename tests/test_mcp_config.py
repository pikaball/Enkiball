from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from enkiball.mcp import MCPManager


class MCPEnvironmentConfigTests(unittest.TestCase):
    def test_environment_is_passed_to_stdio_server(self) -> None:
        client = Mock(running=True, tools=[], resources=[], prompts=[])
        config = {
            "mcp": {
                "servers": {
                    "local": {
                        "command": "node",
                        "args": ["server.js"],
                        "env": {"SHARED": "legacy"},
                        "environment": {"SHARED": "current", "TOKEN": "secret"},
                    }
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
