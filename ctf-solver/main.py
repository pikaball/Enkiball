from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # `python ctf-solver/main.py` has no package context.  Importing through
    # importlib keeps the user-facing directory name while preserving relative
    # imports inside the copied package.
    import importlib

    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root / "src"))
    sys.path.insert(0, str(project_root))
    CtfSolverSupervisor = importlib.import_module(
        "ctf-solver.supervisor"
    ).CtfSolverSupervisor
else:
    from .supervisor import CtfSolverSupervisor

from enkiball import EnkiballCLI


def build_cli(
    *,
    cwd: Path | None = None,
    config_path: Path | None = None,
    sandbox_network: str = "host",
    sandbox: bool = True,
    max_running: int = 10,
    tick_interval: float = 10.0,
    supervisor_model: str | None = None,
    executor_model: str = "moonshot/kimi-k3",
) -> EnkiballCLI:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    supervisor = CtfSolverSupervisor(
        config_path=str(config_path) if config_path else None,
        cwd=str(cwd),
        dir_permissions=[str(cwd)],
        sandbox_network=sandbox_network,
        sandbox=sandbox,
        max_running=max_running,
        tick_interval=tick_interval,
        supervisor_model=supervisor_model,
        executor_model=executor_model,
    )
    cli = EnkiballCLI(
        supervisor.agent,
        banner=(
            "CTF Solver ready. Read api_doc.md and say start; "
            "the contest queue will run autonomously."
        ),
        input_transform=supervisor.inject_user_prompt,
        on_session_change=supervisor.switch_session,
        btw_context=supervisor.workspace.floating_context,
    )
    supervisor.install_cli_commands(cli)
    cli.webhack = supervisor  # type: ignore[attr-defined]
    return cli


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous Enkiball CTF solver")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--sandbox-network",
        choices=("none", "host"),
        default="host",
        help="network available to shell tools (default: host for contest APIs)",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="run shell tools directly on the host",
    )
    parser.add_argument("--max-running", type=int, default=10)
    parser.add_argument("--tick-interval", type=float, default=10.0)
    parser.add_argument(
        "--supervisor-model", default=None,
        help="model for the fast control-plane supervisor (default: active CLI model)",
    )
    parser.add_argument(
        "--executor-model", default="moonshot/kimi-k3",
        help="model for challenge executors (default: moonshot/kimi-k3)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"workspace directory not found: {cwd}")
    cli = build_cli(
        cwd=cwd,
        config_path=args.config.expanduser().resolve() if args.config else None,
        sandbox_network=args.sandbox_network,
        sandbox=not args.no_sandbox,
        max_running=max(1, args.max_running),
        tick_interval=max(1.0, args.tick_interval),
        supervisor_model=args.supervisor_model,
        executor_model=args.executor_model,
    )
    try:
        cli.run()
    finally:
        cli.webhack.shutdown()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
