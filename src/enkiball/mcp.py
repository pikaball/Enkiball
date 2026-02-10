from __future__ import annotations

from typing import Any

from .mcp_client import MCPClient, SSEMCPClient, StdioMCPClient, StreamableHTTPMCPClient


class MCPManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.clients: dict[str, MCPClient] = {}

    def _servers(self) -> dict[str, dict[str, Any]]:
        return self.config.get("mcp", {}).get("servers", {})

    def start(self, name: str) -> tuple[bool, str]:
        if name in self.clients:
            client = self.clients[name]
            if client.running:
                return True, f"{name} already running"
            # If not running, remove and restart
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
                    client = SSEMCPClient(name, item["url"])
            else:
                # Stdio Local Server
                cmd = item.get("command")
                args = item.get("args", [])
                env = item.get("env", {})
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

    def call_tool(self, unique_name: str, args: dict[str, Any]) -> Any:
        # Expected format: mcp__<server>__<tool>
        parts = unique_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError(f"Invalid MCP tool name: {unique_name}")

        server_name = parts[1]
        tool_name = parts[2]

        client = self.clients.get(server_name)
        if not client or not client.running:
            raise RuntimeError(f"MCP server {server_name} not available")

        return client.call_tool(tool_name, args)
