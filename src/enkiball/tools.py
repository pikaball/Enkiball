from __future__ import annotations

import subprocess
from pathlib import Path


def read_file(path: str) -> str:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"File not found: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"Not a file: {target}")
    return target.read_text(encoding="utf-8")


def write_file(path: str, content: str, append: bool = False) -> str:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as f:
        f.write(content)
    return str(target)


def run_command(command: str, timeout_sec: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout, proc.stderr
