"""Provider-independent fallback estimates, never used as reported API usage."""

from __future__ import annotations

from typing import Any

from .message_codec import drop_orphan_tool_call_messages


def estimate_tokens(value: Any) -> int:
    """Approximate text tokens without a model tokenizer or reading image files.

    ASCII text averages roughly four characters per token; use one token per
    non-ASCII character. Images receive a fixed allowance, since their actual
    cost depends on the provider, resolution and detail setting. These are
    heuristics, not a guarantee that a request fits a model's context window.
    """
    if isinstance(value, str):
        ascii_count = sum(char.isascii() for char in value)
        return (ascii_count + 3) // 4 + len(value) - ascii_count
    if isinstance(value, list):
        return sum(estimate_tokens(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in ("input_image", "image_url"):
            return 1024
        return sum(
            estimate_tokens(key) + estimate_tokens(item)
            for key, item in value.items()
            if not key.startswith("_")
        )
    return 0 if value is None else estimate_tokens(str(value))


def estimate_context_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> int:
    visible = drop_orphan_tool_call_messages(messages)
    return 3 + sum(4 + estimate_tokens(msg) for msg in visible) + estimate_tokens(tools)
