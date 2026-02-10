from __future__ import annotations

import json
from typing import Any

from .tools import read_file, run_command, write_file

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
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def dispatch_tool(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "read_file":
            content = read_file(str(args["path"]))
            # Keep tool payload bounded for model context hygiene.
            if len(content) > 20000:
                content = content[:20000] + "\n...[truncated]"
            return json.dumps({"ok": True, "content": content}, ensure_ascii=False)

        if name == "write_file":
            target = write_file(
                path=str(args["path"]),
                content=str(args["content"]),
                append=bool(args.get("append", False)),
            )
            return json.dumps({"ok": True, "path": target}, ensure_ascii=False)

        if name == "run_command":
            timeout_sec = int(args.get("timeout_sec", 120))
            code, out, err = run_command(command=str(args["command"]), timeout_sec=timeout_sec)
            if len(out) > 12000:
                out = out[:12000] + "\n...[truncated]"
            if len(err) > 12000:
                err = err[:12000] + "\n...[truncated]"
            return json.dumps(
                {"ok": True, "exit_code": code, "stdout": out, "stderr": err},
                ensure_ascii=False,
            )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    return json.dumps({"ok": False, "error": f"unknown tool: {name}"}, ensure_ascii=False)
