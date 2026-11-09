from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .mcp_client import MCPClient, SSEMCPClient, StdioMCPClient, StreamableHTTPMCPClient


@dataclass
class MCPServerConfig:
    """Base class for MCP server configuration."""

    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


@dataclass
class StdioMCPServer(MCPServerConfig):
    """Configuration for a local MCP server running via standard I/O."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] | None = None
    transport: Literal["jsonl", "content-length"] = "jsonl"

    def to_dict(self) -> dict[str, Any]:
        env_map = dict(self.environment)
        if self.env:
            env_map.update(self.env)
        return {
            "command": self.command,
            "args": list(self.args),
            "environment": env_map,
            "transport": self.transport,
            "enabled": self.enabled,
        }


@dataclass
class SSEMCPServer(MCPServerConfig):
    """Configuration for a remote MCP server using Server-Sent Events (SSE)."""

    url: str = ""
    headers: dict[str, str] | None = None
    type: str = "sse"

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "url": self.url,
            "type": self.type,
            "enabled": self.enabled,
        }
        if self.headers:
            res["headers"] = dict(self.headers)
        return res


@dataclass
class StreamableHTTPMCPServer(MCPServerConfig):
    """Configuration for a remote MCP server using Streamable HTTP."""

    url: str = ""
    headers: dict[str, str] | None = None
    type: str = "streamable-http"

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "url": self.url,
            "type": self.type,
            "enabled": self.enabled,
        }
        if self.headers:
            res["headers"] = dict(self.headers)
        return res


class MCPManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.clients: dict[str, MCPClient] = {}

    def _servers(self) -> dict[str, dict[str, Any]]:
        return self.config.get("mcp", {}).get("servers", {})

    @staticmethod
    def _environment(item: dict[str, Any]) -> dict[str, str]:
        """Return configured stdio environment with backwards compatibility."""
        legacy = item.get("env", {})
        current = item.get("environment", {})
        if legacy is None:
            legacy = {}
        if current is None:
            current = {}
        if not isinstance(legacy, dict) or not isinstance(current, dict):
            raise ValueError("MCP environment must be an object")

        merged = {**legacy, **current}
        for name, value in merged.items():
            if not isinstance(name, str) or not name or "=" in name or "\0" in name:
                raise ValueError(f"invalid MCP environment variable name: {name!r}")
            if not isinstance(value, str) or "\0" in value:
                raise ValueError(f"invalid MCP environment value for {name!r}")
        return merged

    def add_server(
        self,
        name: str,
        config: MCPServerConfig | Mapping[str, Any],
        start: bool = False,
    ) -> tuple[bool, str]:
        """Add or update an MCP server in configuration and optionally start it."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("server name must be a non-empty string")
        name = name.strip()

        if isinstance(config, MCPServerConfig):
            cfg_dict = config.to_dict()
        elif isinstance(config, Mapping):
            cfg_dict = dict(config)
        else:
            raise TypeError("config must be an MCPServerConfig or mapping")

        servers = self.config.setdefault("mcp", {}).setdefault("servers", {})
        servers[name] = cfg_dict

        if start:
            return self.start(name)
        return True, f"server '{name}' added"

    def remove_server(self, name: str) -> bool:
        """Stop and remove a server from configuration and active clients."""
        servers = self._servers()
        existed = name in servers or name in self.clients
        self.stop(name)
        servers.pop(name, None)
        return existed

    def register_client(
        self,
        name: str,
        client: MCPClient,
        start: bool = True,
    ) -> tuple[bool, str]:
        """Directly register an existing MCPClient instance without modifying config."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("client name must be a non-empty string")
        name = name.strip()
        if not isinstance(client, MCPClient):
            raise TypeError(f"expected MCPClient instance, got {type(client).__name__}")

        if name in self.clients:
            old = self.clients[name]
            if old is not client and old.running:
                old.close()

        if start and not client.running:
            try:
                client.start()
            except Exception as e:
                return False, f"{name} failed to start: {e}"

        self.clients[name] = client

        summary = []
        if client.tools:
            summary.append(f"{len(client.tools)} tools")
        if client.resources:
            summary.append(f"{len(client.resources)} resources")
        if client.prompts:
            summary.append(f"{len(client.prompts)} prompts")
        summary_str = ", ".join(summary) if summary else "no capabilities"
        return True, f"{name} registered with {summary_str}"

    def start(self, name: str) -> tuple[bool, str]:
        if name in self.clients:
            client = self.clients[name]
            if client.running:
                return True, f"{name} already running"
            client.close()
            self.clients.pop(name)

        servers = self._servers()
        if name not in servers:
            return False, f"unknown MCP server: {name}"

        item = servers[name]
        server_type = str(item.get("type", "")).strip().lower()

        try:
            if "url" in item:
                headers = item.get("headers")
                if server_type in {"streamable-http", "streamable_http", "http"}:
                    client = StreamableHTTPMCPClient(name, item["url"], headers=headers)
                else:
                    # Default remote type: SSE
                    client = SSEMCPClient(name, item["url"], headers=headers)
            else:
                # Stdio Local Server
                cmd = item.get("command")
                args = item.get("args", [])
                env = self._environment(item)
                transport = item.get("transport", "jsonl")
                if not cmd:
                    return False, f"MCP {name} missing command or url"
                client = StdioMCPClient(name, cmd, args, env, transport=transport)

            client.start()
            self.clients[name] = client

            summary = []
            if client.tools:
                summary.append(f"{len(client.tools)} tools")
            if client.resources:
                summary.append(f"{len(client.resources)} resources")
            if client.prompts:
                summary.append(f"{len(client.prompts)} prompts")

            summary_str = ", ".join(summary) if summary else "no capabilities"
            return True, f"{name} started with {summary_str}"

        except Exception as e:
            return False, f"{name} failed to start: {e}"

    def start_enabled(self) -> list[str]:
        messages: list[str] = []
        for name, item in self._servers().items():
            if not item.get("enabled"):
                continue
            _, msg = self.start(name)
            messages.append(msg)
        return messages

    def stop(self, name: str) -> tuple[bool, str]:
        client = self.clients.get(name)
        if not client:
            return True, f"{name} not running"

        client.close()
        self.clients.pop(name, None)
        return True, f"{name} stopped"

    def stop_all(self) -> list[str]:
        messages: list[str] = []
        for name in list(self.clients):
            _, msg = self.stop(name)
            messages.append(msg)
        return messages

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        # Check configured servers
        for name in self._servers():
            if name in self.clients and self.clients[name].running:
                out[name] = "RUNNING"
            else:
                out[name] = "STOPPED"

        # Check dynamic/remote servers not in config
        for name, client in self.clients.items():
            if name not in out:
                out[name] = (
                    "RUNNING (Remote)"
                    if isinstance(client, (SSEMCPClient, StreamableHTTPMCPClient))
                    else "RUNNING"
                )

        return out

    def get_tools(self) -> list[dict[str, Any]]:
        all_tools: list[dict[str, Any]] = []
        for client in self.clients.values():
            if not client.running:
                continue
            for tool in client.tools:
                tool_name = tool.get("name")
                if not tool_name:
                    continue
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}, "additionalProperties": True}

                # MCP uses `inputSchema`; OpenAI tools require `parameters`.
                openai_fn = {
                    "name": f"mcp__{client.name}__{tool_name}",
                    "description": tool.get("description", ""),
                    "parameters": schema,
                }
                all_tools.append({"type": "function", "function": openai_fn})
        return all_tools

    def call_tool(
        self, unique_name: str, args: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Any:
        # Expected format: mcp__<server>__<tool>
        parts = unique_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError(f"Invalid MCP tool name: {unique_name}")

        server_name = parts[1]
        tool_name = parts[2]

        client = self.clients.get(server_name)
        if not client or not client.running:
            raise RuntimeError(f"MCP server {server_name} not available")

        return client.call_tool(tool_name, args, cancel_event=cancel_event)
