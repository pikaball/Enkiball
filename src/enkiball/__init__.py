"""Enkiball – Python LLM foundation.

Public API::

    from enkiball import Enkiball, ChatResult

    with Enkiball() as agent:
        result = agent.chat("hello")
        print(result)
"""

from .agent import ChatResult, Enkiball
from .cli import EnkiballCLI
from .commands import CommandContext, CommandHandler
from .llm import DEFAULT_COMPACT_PROMPT, LiveSnapshot, RetryEvent, TokenUsage
from .llm_tools import (
    BUILTIN_TOOL_NAMES,
    ToolCallContext,
    ToolHook,
    ToolResultContext,
    generate_tool_schema,
)
from .mcp import (
    MCPManager,
    MCPServerConfig,
    SSEMCPServer,
    StdioMCPServer,
    StreamableHTTPMCPServer,
)
from .mcp_client import (
    MCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHTTPMCPClient,
)
from .sandbox import EnkiSandbox, PermissionDeniedError, SandboxUnavailableError
from .session_store import SessionRecord, SessionStore
from .skills import Skill, SkillRegistry

__all__ = [
    "Enkiball",
    "EnkiSandbox",
    "PermissionDeniedError",
    "SandboxUnavailableError",
    "EnkiballCLI",
    "CommandHandler",
    "CommandContext",
    "ChatResult",
    "LiveSnapshot",
    "RetryEvent",
    "DEFAULT_COMPACT_PROMPT",
    "TokenUsage",
    "SessionRecord",
    "SessionStore",
    "Skill",
    "SkillRegistry",
    # MCP
    "MCPManager",
    "MCPClient",
    "StdioMCPClient",
    "SSEMCPClient",
    "StreamableHTTPMCPClient",
    "MCPServerConfig",
    "StdioMCPServer",
    "SSEMCPServer",
    "StreamableHTTPMCPServer",
    # Tools & Lifecycle
    "BUILTIN_TOOL_NAMES",
    "ToolCallContext",
    "ToolResultContext",
    "ToolHook",
    "generate_tool_schema",
]
