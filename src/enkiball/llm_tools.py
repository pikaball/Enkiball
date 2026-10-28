from __future__ import annotations

import json
import threading
from typing import Any

from .sandbox import Sandbox, TurnInterrupted
from .tools import DEFAULT_READ_END, edit_file, read_file, read_image, write_file


class ImageToolResult(str):
    """Keep binary content separate from textual callbacks and system notices."""

    def __new__(cls, text: str, image_url: str) -> ImageToolResult:
        result = super().__new__(cls, text)
        result.image_url = image_url
        return result

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": (
                "Read a local image and attach its visual content for inspection by "
                "a vision-capable model. Supports PNG, JPEG, GIF and WebP up to 20 MiB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path or path relative to the working directory."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a character range from a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to target file."},
                    "start": {
                        "type": "integer",
                        "description": "Inclusive character offset.",
                        "minimum": 0,
                        "default": 0,
                    },
                    "end": {
                        "type": "integer",
                        "description": "Exclusive character offset.",
                        "minimum": 0,
                        "default": DEFAULT_READ_END,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to target file."},
                    "content": {"type": "string", "description": "Text to write."},
                    "append": {
                        "type": "boolean",
                        "description": "When true append to file, otherwise overwrite.",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": (
                "Replace exact text in an existing UTF-8 file. old_text must match "
                "exactly once unless replace_all is true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to target file."},
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace, including whitespace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text; use an empty string to delete.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence instead of requiring a unique match.",
                        "default": False,
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run /bin/sh -c in the configured working directory and capture "
                "exit_code/stdout/stderr. By default commands run inside a Linux "
                "bubblewrap sandbox: only permitted directories and read-only system "
                "runtime paths are visible; network is isolated unless configured "
                "otherwise; environment variables are filtered, HOME is temporary, "
                "and /tmp is private to each command unless explicitly mounted. "
                "Writes to writable mounted directories persist. Missing host files, "
                "credentials, network access or services can be sandbox restrictions. "
                "When the task requires access blocked by isolation, request a single "
                "host execution with privilleged=true if approval is available. You "
                "may request this directly when the need is known, or retry after "
                "diagnosing a sandbox-related failure; do not repeatedly retry the "
                "same blocked command. A nonzero exit_code does not by itself prove "
                "a sandbox issue. Respect a denied approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Timeout in seconds.",
                        "minimum": 1,
                        "maximum": 600,
                        "default": 120,
                    },
                    "privilleged": {
                        "type": "boolean",
                        "description": (
                            "Request one-time approval to run directly on the host "
                            "without sandbox isolation, using the host environment "
                            "and network as the current user (not sudo/root). Set true "
                            "for required host-only files, services, credentials or "
                            "network access unavailable in the sandbox, or when the "
                            "sandbox backend is unavailable. This tool call triggers "
                            "approval; a text-only request does not. Each host command "
                            "requires fresh approval while sandboxing is enabled. "
                            "The parameter name is spelled privilleged."
                        ),
                        "default": False,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def dispatch_tool(
    name: str,
    args: dict[str, Any],
    sandbox: Sandbox | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    sandbox = sandbox or Sandbox()
    try:
        if not isinstance(args, dict):
            raise TypeError("tool arguments must be an object")
        if name == "read_image":
            path = args.get("path")
            if not isinstance(path, str):
                raise TypeError("path must be a string")
            if not path.strip():
                raise ValueError("path must not be empty")
            target = sandbox.resolve_path(path, "read")
            image_url = read_image(str(target))
            return ImageToolResult(
                json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False),
                image_url,
            )

        if name == "read_file":
            path = args.get("path")
            start = args.get("start", 0)
            end = args.get("end", DEFAULT_READ_END)
            if not isinstance(path, str):
                raise TypeError("path must be a string")
            if isinstance(start, bool) or not isinstance(start, int):
                raise TypeError("start must be an integer")
            if isinstance(end, bool) or not isinstance(end, int):
                raise TypeError("end must be an integer")
            if start < 0:
                raise ValueError("start must be non-negative")
            if end < start:
                raise ValueError("end must be greater than or equal to start")
            target = sandbox.resolve_path(path, "read")
            content = read_file(str(target), start=start, end=end)
            return json.dumps({"ok": True, "content": content}, ensure_ascii=False)

        if name == "write_file":
            path = args.get("path")
            content_arg = args.get("content")
            append = args.get("append", False)
            if not isinstance(path, str):
                raise TypeError("path must be a string")
            if not isinstance(content_arg, str):
                raise TypeError("content must be a string")
            if not isinstance(append, bool):
                raise TypeError("append must be a boolean")
            target_path = sandbox.resolve_path(path, "write")
            target = write_file(
                path=str(target_path),
                content=content_arg,
                append=append,
            )
            return json.dumps({"ok": True, "path": target}, ensure_ascii=False)

        if name == "edit":
            for field in ("path", "old_text", "new_text"):
                if not isinstance(args.get(field), str):
                    raise TypeError(f"{field} must be a string")
            replace_all = args.get("replace_all", False)
            if not isinstance(replace_all, bool):
                raise TypeError("replace_all must be a boolean")
            target_path = sandbox.resolve_path(args["path"], "edit")
            target, replacements = edit_file(
                path=str(target_path),
                old_text=args["old_text"],
                new_text=args["new_text"],
                replace_all=replace_all,
            )
            return json.dumps(
                {"ok": True, "path": target, "replacements": replacements},
                ensure_ascii=False,
            )

        if name == "run_command":
            command = args.get("command")
            timeout_sec = args.get("timeout_sec", 120)
            privileged = args.get("privilleged", False)
            if not isinstance(command, str):
                raise TypeError("command must be a string")
            if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int):
                raise TypeError("timeout_sec must be an integer")
            if not isinstance(privileged, bool):
                raise TypeError("privilleged must be a boolean")
            code, out, err = sandbox.run(
                command=command,
                timeout_sec=timeout_sec,
                privileged=privileged,
                cancel_event=cancel_event,
            )
            if len(out) > 12000:
                out = out[:12000] + "\n...[truncated]"
            if len(err) > 12000:
                err = err[:12000] + "\n...[truncated]"
            return json.dumps(
                {"ok": True, "exit_code": code, "stdout": out, "stderr": err},
                ensure_ascii=False,
            )
    except TurnInterrupted:
        raise
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    return json.dumps({"ok": False, "error": f"unknown tool: {name}"}, ensure_ascii=False)
