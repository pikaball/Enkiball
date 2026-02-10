# Enkiball Architecture

## Runtime Layers

1. Entry and shell
   - `src/enkiball/cli.py`: interactive terminal loop, prompt, streaming print, slash command routing.

2. Command/control layer
   - `src/enkiball/commands.py`: `/model`, `/session`, `/mcp` management surface.

3. LLM runtime
   - `src/enkiball/llm.py`: chat session state, token streaming, tool-call loop orchestration.
   - `src/enkiball/llm_tools.py`: built-in local tools exposed to LLM (`read_file`, `write_file`, `run_command`).

4. MCP integration
   - `src/enkiball/mcp.py`: MCP lifecycle and tool exposure bridge.
   - `src/enkiball/mcp_client.py`: transport clients:
     - `StdioMCPClient` (local process; `jsonl` and `content-length`)
     - `SSEMCPClient` (SSE endpoint + POST endpoint)
     - `StreamableHTTPMCPClient` (streamable HTTP)

5. Configuration
   - `src/enkiball/config.py`: load/merge/save config and MCP enable toggles.
   - `config/agent.json`: runtime config source.
6. Session persistence
   - `src/enkiball/session_store.py`: session lifecycle, local persistence, export.
   - `data/sessions/`: per-session message snapshots.

## Current Execution Flow

1. `cli.main()` loads config and starts enabled MCP servers.
2. User text:
   - slash command -> `CommandHandler` (`/session` can switch active conversation)
   - normal chat -> `ChatEngine.stream_reply()`
3. `ChatEngine` calls model with local + MCP tools.
4. If model emits tool-calls:
   - local tool -> `llm_tools.dispatch_tool()`
   - MCP tool -> `MCPManager.call_tool()`
5. Tool result is appended as `role=tool`, loop continues until plain assistant content is returned.
6. Conversation is saved to session file after each turn.

## Design Constraints (Current)

- Single-process runtime with in-memory conversation history.
- No persistent task graph yet (README vision not implemented yet).
- MCP health/reconnect is best-effort, not supervised yet.
