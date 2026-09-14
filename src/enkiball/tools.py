from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

DEFAULT_READ_END = 1000
MAX_LENGTH = 50000
MAX_EDIT_FILE_SIZE = 100000


def read_file(path: str, start: int = 0, end: int = DEFAULT_READ_END) -> str:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"File not found: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"Not a file: {target}")
    if isinstance(start, bool) or not isinstance(start, int):
        raise TypeError("start must be an integer")
    if isinstance(end, bool) or not isinstance(end, int):
        raise TypeError("end must be an integer")
    if start < 0:
        raise ValueError("start must be non-negative")
    if end < start:
        raise ValueError("end must be greater than or equal to start")
    if end - start > MAX_LENGTH:
        raise ValueError(f"length must be less than {MAX_LENGTH} for each read_file tool call")
    with target.open("r", encoding="utf-8") as f:
        remaining = start
        while remaining:
            skipped = f.read(min(remaining, 8192))
            if not skipped:
                return ""
            remaining -= len(skipped)
        return f.read(end - start)


def write_file(path: str, content: str, append: bool = False) -> str:
    target = Path(path).expanduser().resolve()
    content_size = len(content.encode("utf-8"))
    existing_size = target.stat().st_size if append and target.exists() else 0
    if existing_size + content_size > MAX_LENGTH:
        raise ValueError(f"Write exceeds {MAX_LENGTH} byte limit: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as f:
        f.write(content)
    return str(target)


def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> tuple[str, int]:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"File not found: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"Not a file: {target}")
    if old_text == "":
        raise ValueError("old_text must not be empty")
    if old_text == new_text:
        raise ValueError("old_text and new_text must be different")

    if os.path.getsize(target) > MAX_EDIT_FILE_SIZE:
        raise ValueError(f"filesize must be less than {MAX_EDIT_FILE_SIZE} bytes")

    with target.open("r", encoding="utf-8", newline="") as f:
        content = f.read()

    occurrences = content.count(old_text)
    if occurrences == 0:
        raise ValueError(
            "old_text was not found; it must match exactly, including whitespace and line endings"
        )
    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"old_text matched {occurrences} times; add surrounding context or set replace_all"
        )

    replacement_count = occurrences if replace_all else 1
    updated = content.replace(old_text, new_text, replacement_count)
    original_mode = stat.S_IMODE(target.stat().st_mode)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return str(target), replacement_count
