"""Entry point and feature assembly for the installed ``enkiball`` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from ..agent import Enkiball
from ..cli import EnkiballCLI
from ..config import DEFAULT_CONFIG_PATH
from ..session_store import SessionStore
from .orchestration import AgentPythonRuntime
from .session_title import SessionTitleHook
from .sandbox_info import SANDBOX_HELP, SYSTEM_HELP, sandbox_command, sandbox_report
from .agent_display import AgentTaskDisplay
from .session_search import print_search_results, search_sessions


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Suppressed defaults let common options appear on either side of a
    # subcommand without its absent options overwriting the root's values.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    general = common.add_argument_group("application")
    sandbox = common.add_argument_group("tool sandbox (requested settings)")
    general.add_argument(
        "--config",
        type=Path,
        help=f"config file (default: {DEFAULT_CONFIG_PATH})",
    )
    general.add_argument("--cwd", type=Path,
                         help="working directory and default writable root (default: current directory; ignored when resuming a session)")
    sandbox.add_argument(
        "--dir-permission",
        action="append",
        metavar="DIR",
        help="add a read/write directory grant; relative to --cwd; repeatable",
    )
    sandbox.add_argument(
        "--ro-permission",
        action="append",
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
        help="request isolated (none) or shared host networking for built-in tools (default: none)",
    )
    sandbox.add_argument(
        "--sandbox-env",
        action="append",
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
    parser = argparse.ArgumentParser(
        parents=[common],
        description="Enkiball system CLI: interactive agent with Python subagent orchestration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=SANDBOX_HELP + "\nExamples:\n"
        '  enkiball exec "Summarize this project"\n'
        "  enkiball resume SESSION_ID\n"
        '  enkiball exec "Continue the task" --session SESSION_ID\n'
        '  enkiball search "keyword" --global\n'
        "  enkiball --sandbox-info\n"
        "  enkiball --cwd ./project --ro-permission ../reference\n",
    )
    commands = parser.add_subparsers(dest="command", title="commands")
    execute = commands.add_parser("exec", parents=[common],
                                  help="execute one prompt and exit without interactive input")
    execute.add_argument("exec_prompt", metavar="PROMPT", help="the literal prompt to execute")
    execute.add_argument("--session", dest="resume", metavar="SESSION_ID",
                         help="continue this session using its saved cwd")
    resume = commands.add_parser("resume", parents=[common],
                                 help="resume a session interactively using its saved cwd")
    resume.add_argument("resume", metavar="SESSION_ID", help="the exact saved session ID")
    search = commands.add_parser("search", parents=[common],
                                 help="search saved conversations without starting an agent")
    search.add_argument("keyword", metavar="KEYWORD", help="case-insensitive literal substring")
    search.add_argument("--global", dest="global_search", action="store_true",
                        help="search sessions from all working directories")
    search.add_argument("--include-tool", action="store_true",
                        help="also match tool names, arguments and results")
    search.add_argument("--include-thinking", action="store_true",
                        help="also match saved assistant reasoning")
    defaults = dict(
        config=None, cwd=Path.cwd(), dir_permission=[], ro_permission=[],
        no_approval=False, sandbox_network="none", sandbox_env=[],
        privileged=False, sandbox_info=False, exec_prompt=None, resume=None,
    )
    # Parse common options together so repeatable grants also accumulate when
    # split across both sides of a subcommand. `--` still protects literal input.
    common_args, remaining = common.parse_known_args(argv)
    args = argparse.Namespace(**(defaults | vars(common_args) | vars(parser.parse_args(remaining))))
    if args.exec_prompt is not None and not args.exec_prompt.strip():
        parser.error("exec requires a non-empty prompt")
    if args.command == "search" and not args.keyword.strip():
        parser.error("search requires a non-empty keyword")
    if args.sandbox_info and args.command is not None:
        parser.error("--sandbox-info cannot be combined with a subcommand")
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "search":
        results = search_sessions(
            SessionStore(cwd=args.cwd), args.keyword,
            global_search=args.global_search,
            include_tool=args.include_tool, include_thinking=args.include_thinking,
        )
        print_search_results(Console(), results, args.keyword)
        return
    if args.resume is not None:
        try:
            # Resolve the explicit config against the launch directory, then
            # adopt the saved cwd before constructing tools or starting MCP.
            if args.config is not None:
                args.config = args.config.expanduser().resolve()
            record = SessionStore().load(args.resume, ignore_cwd=True)
            if not record.cwd or not Path(record.cwd).is_absolute():
                raise ValueError("session has no absolute saved cwd")
            args.cwd = Path(record.cwd).resolve()
            os.chdir(args.cwd)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            print(f"Cannot resume session {args.resume!r}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    options = dict(
        cwd=args.cwd,
        dir_permissions=[Path(args.cwd).resolve().anchor]
        if args.privileged else args.dir_permission,
        approval=not args.no_approval and args.exec_prompt is None,
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
            if args.exec_prompt is None:
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
    with patch_stdout(raw=True) if args.exec_prompt is None else nullcontext():
        try:
            run_options = {}
            if args.exec_prompt is not None:
                run_options["exec_prompt"] = args.exec_prompt
            if args.resume is not None:
                run_options["resume_session_id"] = args.resume
            status = cli.run(**run_options)
            if args.exec_prompt is not None and status:
                raise SystemExit(status)
        finally:
            runtime.close()
