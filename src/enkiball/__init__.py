"""Enkiball – Python LLM foundation.

Public API::

    from enkiball import Enkiball, ChatResult

    with Enkiball() as agent:
        result = agent.chat("hello")
        print(result)
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .agent import ChatResult, Enkiball
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

if TYPE_CHECKING:
    from .cli import CliTurnContext, EnkiballCLI
    from .commands import CommandContext, CommandHandler

_CLI_EXPORTS = {
    "CliTurnContext": ".cli",
    "EnkiballCLI": ".cli",
    "CommandContext": ".commands",
    "CommandHandler": ".commands",
}


def __getattr__(name: str) -> Any:
    module = _CLI_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(globals().keys() | _CLI_EXPORTS.keys())

__all__ = [
    "Enkiball",
    "EnkiSandbox",
    "PermissionDeniedError",
    "SandboxUnavailableError",
    "EnkiballCLI",
    "CliTurnContext",
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
