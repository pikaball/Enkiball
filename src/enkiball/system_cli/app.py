"""Entry point and feature assembly for the installed ``enkiball`` command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from ..agent import Enkiball
from ..cli import EnkiballCLI
from ..config import DEFAULT_CONFIG_PATH
from .orchestration import AgentPythonRuntime
from .session_title import SessionTitleHook
from .sandbox_info import SANDBOX_HELP, SYSTEM_HELP, sandbox_command, sandbox_report
from .agent_display import AgentTaskDisplay


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enkiball system CLI: interactive agent with Python subagent orchestration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=SANDBOX_HELP + "\nExamples:\n"
        "  enkiball --sandbox-info\n"
        "  enkiball --cwd ./project --ro-permission ../reference\n"
        "  enkiball --sandbox-network host --sandbox-env HTTP_PROXY\n"
        "  enkiball --privileged\n",
    )
    general = parser.add_argument_group("application")
    sandbox = parser.add_argument_group("tool sandbox (requested settings)")
    general.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config file (default: {DEFAULT_CONFIG_PATH})",
    )
    general.add_argument("--cwd", type=Path, default=Path.cwd(),
                         help="working directory and default writable root (default: current directory)")
    sandbox.add_argument(
        "--dir-permission",
        action="append",
        default=[],
        metavar="DIR",
        help="add a read/write directory grant; relative to --cwd; repeatable",
    )
    sandbox.add_argument(
        "--ro-permission",
        action="append",
        default=[],
        metavar="DIR",
        help="add a read-only directory grant; relative to --cwd; repeatable",
    )
    sandbox.add_argument(
        "--no-approval",
        action="store_true",
        help="deny new directory/host-execution approvals; keep existing grants (default: prompt)",
    )
    sandbox.add_argument(
        "--sandbox-network",
        choices=("none", "host"),
        default="none",
        help="request isolated (none) or shared host networking for built-in tools (default: none)",
    )
    sandbox.add_argument(
        "--sandbox-env",
        action="append",
        default=[],
        metavar="NAME",
        help="add a host variable to the sandbox's minimal environment; repeatable (default: no extras)",
    )
    sandbox.add_argument(
        "--privileged", "--privilleged",
        dest="privileged",
        action="store_true",
        help="force host execution and grant file tools access to /; no root elevation (legacy alias: --privilleged)",
    )
    sandbox.add_argument(
        "--sandbox-info", action="store_true",
        help="print requested/effective sandbox settings as JSON and exit; no LLM or MCP startup",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    options = dict(
        cwd=args.cwd,
        dir_permissions=[Path(args.cwd).resolve().anchor]
        if args.privileged else args.dir_permission,
        approval=not args.no_approval,
        sandbox=not args.privileged,
        sandbox_network=args.sandbox_network,
        sandbox_inherit_env=args.sandbox_env,
        ro_permissions=args.ro_permission,
    )
    if args.sandbox_info:
        # Sandbox flags are independent of provider credentials and saved state.
        with Enkiball(config={}, persist=False, auto_start_mcp=False, **options) as agent:
            records = Path.home() / ".config" / "enkiball" / "runs"
            if records.is_dir():
                agent._sandbox.set_managed_read_roots("orchestration_traces", [records])
            report = sandbox_report(agent)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["effective"]["execution_mode"] == "unavailable":
            raise SystemExit(1)
        return
    try:
        agent = Enkiball(
            config_path=args.config,
            **options,
            auto_start_mcp=True,
        )
        console = Console()
        display = AgentTaskDisplay(console, agent.cwd)
        runtime = AgentPythonRuntime(agent, on_event=display.on_event, wait_context=display.waiting)
        display.runtime = runtime
        title_hook = SessionTitleHook()

        def on_turn_end(context):
            runtime.on_turn_end(context)
            title_hook(context)

        def on_session_change(session_id):
            display.set_session(session_id)
            runtime.on_session_change(session_id)

        cli = EnkiballCLI(
            agent,
            console=console,
            on_turn_end=on_turn_end,
            on_session_change=on_session_change,
            poll_background=runtime.poll_background,
            on_background_turn=runtime.on_background_turn,
            tool_renderers=display.tool_renderers,
        )
        cli.handler.help_footer = SYSTEM_HELP
        cli.register_command("sandbox", sandbox_command,
                             "Show requested and actual sandbox settings; /sandbox help for details",
                             completions=["help"])
        cli.register_command("agents", display.command,
                             "Show workflows; /agents help for results, code, traces and cancel",
                             completions=["help", "code", "logs", "trace", "cancel"])
    except FileNotFoundError:
        raise SystemExit(1) from None
    # Use one output queue across prompts, model turns and worker cleanup, so
    # background lifecycle messages stay ordered and preserve the input draft.
    with patch_stdout(raw=True):
        try:
            cli.run()
        finally:
            runtime.close()
