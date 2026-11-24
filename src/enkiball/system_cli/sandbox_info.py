"""Sandbox reporting and help for the system application."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from ..agent import Enkiball


SANDBOX_HELP = """Sandbox behavior:
  By default, request a sandbox for built-in file/shell tools, with cwd writable,
  discovered skill directories readable, isolated networking, and a minimal
  environment. Extra directory grants are relative to --cwd.
  These are requested settings: they require enkisandbox-client and its backend.
  Missing client => host execution, host networking/environment, no kernel
  sandbox policy. File-tool SDK path checks still apply. A present but failing
  client/backend raises an error; it does not fall back to host execution.
  --no-approval disables new interactive grants; it does not enforce isolation.
  --privileged explicitly selects host mode; it does not grant root privileges.

Scope:
  --sandbox-network and --sandbox-env apply to sandboxed built-in tools, not LLM
  API calls, MCP servers, /debug, or orchestrate's Python process. Those run in
  the host harness or their own service. Child Agent tools inherit the requested
  sandbox settings by default, with interactive approvals disabled.
  CPU/memory, device, capability, seccomp and CIDR policies are available via the
  Python EnkiSandbox API; this CLI does not expose flags for them yet.

Runtime discovery:
  ENKISANDBOX_CLIENT  Client executable or path (default: enkisandbox-client)
  ENKISANDBOX_SOCKET  Backend socket (default: /run/enkisandbox/control.sock)
  --sandbox-info or /sandbox shows requested settings and the actual runtime.
"""

SYSTEM_HELP = """System CLI features:
  /sandbox shows requested/effective sandbox settings; /sandbox help explains
  startup sandbox flags and their scope. Use enkiball --help for all flags.
  /agents shows live/recent task status; /agents <id> shows results. Use
  /agents code|logs|trace|cancel <id> to inspect or stop a task; /agents help for details.
  New unnamed sessions receive a title after their first turn, including interrupts.
  orchestrate(code, wait=true|false) and cancel_run are model tools, not slash
  commands. Python workflows are one-shot host processes. Child Agent tools
  inherit the requested sandbox settings. /agents trace <id> locates saved records.
"""


def sandbox_report(agent: Enkiball) -> dict[str, Any]:
    """Show requested and actual policies without printing environment values."""
    adapter = agent._sandbox
    client = os.environ.get("ENKISANDBOX_CLIENT", "enkisandbox-client")
    report: dict[str, Any] = {
        "requested": {
            "cwd": str(agent.cwd),
            "sandbox_enabled": adapter.enabled,
            "approval_enabled": adapter.approval,
            "network": adapter.network,
            "writable_directories": [str(p) for p in adapter.writable_directories],
            "read_only_directories": [str(p) for p in adapter.read_only_directories],
            "inherit_env_names": list(adapter._inherit_env),
            "explicit_env_names": sorted(adapter._env),
            "resources": adapter._policy.get("resources", {}),
        },
        "backend": {
            "client_requested": client,
            "client_resolved": shutil.which(client),
            "socket": os.environ.get("ENKISANDBOX_SOCKET", "/run/enkisandbox/control.sock"),
        },
    }
    try:
        effective = agent.sandbox_context
    except Exception as exc:
        effective = {"execution_mode": "unavailable", "error": str(exc)}
    report["effective"] = effective
    mode = effective["execution_mode"]
    report["mode_reason"] = (
        "Sandbox backend is active."
        if mode == "sandbox" else
        "Sandbox initialization failed; no host fallback."
        if mode == "unavailable" else
        "Host execution was explicitly selected."
        if not adapter.enabled else
        "Sandbox client is missing; built-in tools use host execution."
    )
    report["scope"] = {
        "builtin_file_tools": "SDK path checks, plus the backend policy when active",
        "builtin_shell_tools": "backend policy when active; host access otherwise",
        "llm_and_mcp": "host harness / configured service; not covered by these sandbox flags",
        "orchestrate_and_debug": "host Python; not covered by the tool sandbox",
        "child_agent_tools": "inherit requested tool settings; no interactive approval",
    }
    return report


def sandbox_command(argv, context) -> bool:
    if len(argv) == 2 and argv[1] == "help":
        context.console.print(SANDBOX_HELP, markup=False)
    elif len(argv) == 1:
        context.console.print(json.dumps(sandbox_report(context.agent), ensure_ascii=False, indent=2),
                              markup=False)
    else:
        context.console.print("usage: /sandbox [help]", style="yellow", markup=False)
    return True
