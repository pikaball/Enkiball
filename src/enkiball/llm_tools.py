from __future__ import annotations

import json
import threading
from typing import Any

from .sandbox import Sandbox, TurnInterrupted
from .tools import edit_file, read_file, write_file

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to target file."},
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
            "description": "Run a shell command and capture exit_code/stdout/stderr.",
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
                            "without sandbox isolation."
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
        if name == "read_file":
            path = args.get("path")
            if not isinstance(path, str):
                raise TypeError("path must be a string")
            target = sandbox.resolve_path(path, "read")
            content = read_file(str(target))
            # Keep tool payload bounded for model context hygiene.
            if len(content) > 20000:
                content = content[:20000] + "\n...[truncated]"
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
