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
from .session_store import SessionRecord, SessionStore
from .skills import Skill, SkillRegistry

__all__ = [
    "Enkiball",
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
]
