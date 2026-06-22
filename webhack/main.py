from __future__ import annotations

import sys
from pathlib import Path

from enkiball import EnkiballCLI

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from webhack.supervisor import WebHackSupervisor
else:
    from .supervisor import WebHackSupervisor


def build_cli() -> EnkiballCLI:
    supervisor = WebHackSupervisor()
    cli = EnkiballCLI(
        supervisor.agent,
        banner="WebHack Supervisor ready. Use /help, /task, /exec, /state, /btw.",
        input_transform=supervisor.inject_user_prompt,
        on_session_change=supervisor.switch_session,
    )
    supervisor.install_cli_commands(cli)
    # Keep a reference so callers embedding the CLI can reach orchestration.
    cli.webhack = supervisor  # type: ignore[attr-defined]
    return cli


def main() -> None:
    cli = build_cli()
    try:
        # The watchdog loop is opt-in via /loop start. Starting it before the
        # human has decomposed/confirmed scope can make the supervisor compete
        # with normal chat turns and feel like a hang.
        cli.run()
    finally:
        cli.webhack.shutdown()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
