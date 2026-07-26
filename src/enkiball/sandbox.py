from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Iterable


class PermissionDeniedError(PermissionError):
    pass


class SandboxUnavailableError(RuntimeError):
    pass


ApprovalHandler = Callable[[Path, str], bool]
MAX_OUTPUT_BYTES = 1_000_000


class Sandbox:
    def __init__(
        self,
        cwd: str | Path | None = None,
        dir_permissions: Iterable[str | Path] | None = None,
        approval: bool = False,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        if not self.cwd.is_dir():
            raise NotADirectoryError(f"Working directory not found: {self.cwd}")
        self.approval = bool(approval)
        self._roots = {self.cwd}
        self._roots_lock = threading.RLock()
        self._approval_handler: ApprovalHandler | None = None
        for path in dir_permissions or ():
            self._roots.add(self._resolve_root(path))

    @property
    def allowed_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._roots, key=str))

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def resolve_path(self, path: str | Path, operation: str) -> Path:
        raw = Path(path).expanduser()
        target = raw if raw.is_absolute() else self.cwd / raw
        target = target.resolve(strict=False)
        if self._is_allowed(target):
            return target

        requested_root = target if target.is_dir() else target.parent
        if (
            self.approval
            and self._approval_handler is not None
            and self._approval_handler(requested_root, operation)
        ):
            with self._roots_lock:
                self._roots.add(requested_root)
            return target
        raise PermissionDeniedError(
            f"{operation} denied outside allowed directories: {target}"
        )

    def run(self, command: str, timeout_sec: int = 120) -> tuple[int, str, str]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if isinstance(timeout_sec, bool) or not 1 <= timeout_sec <= 600:
            raise ValueError("timeout_sec must be between 1 and 600")

        argv = self._build_bwrap_command(command)
        process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=self._sandbox_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stdout_thread = self._drain_output(process.stdout, stdout_parts)
        stderr_thread = self._drain_output(process.stderr, stderr_parts)
        try:
            process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise subprocess.TimeoutExpired(
                command,
                timeout_sec,
                self._decode_output(stdout_parts),
                self._decode_output(stderr_parts),
            ) from None
        stdout_thread.join()
        stderr_thread.join()
        return (
            process.returncode,
            self._decode_output(stdout_parts),
            self._decode_output(stderr_parts),
        )

    @staticmethod
    def _drain_output(
        stream: BinaryIO,
        parts: list[bytes],
    ) -> threading.Thread:
        def _read() -> None:
            remaining = MAX_OUTPUT_BYTES + 1
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if remaining > 0:
                        kept = chunk[:remaining]
                        parts.append(kept)
                        remaining -= len(kept)
            finally:
                stream.close()

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _decode_output(parts: list[bytes]) -> str:
        content = b"".join(parts)
        truncated = len(content) > MAX_OUTPUT_BYTES
        decoded = content[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return decoded + "\n...[truncated]" if truncated else decoded

    def _resolve_root(self, path: str | Path) -> Path:
        raw = Path(path).expanduser()
        root = raw if raw.is_absolute() else self.cwd / raw
        root = root.resolve(strict=False)
        if not root.is_dir():
            raise NotADirectoryError(f"Allowed directory not found: {root}")
        return root

    def _is_allowed(self, target: Path) -> bool:
        with self._roots_lock:
            return any(
                target == root or target.is_relative_to(root) for root in self._roots
            )

    def _build_bwrap_command(self, command: str) -> list[str]:
        if platform.system() != "Linux":
            raise SandboxUnavailableError("exec sandbox currently requires Linux")
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise SandboxUnavailableError("bubblewrap is required for command execution")

        argv = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
            "--tmpfs",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for path in self._runtime_paths():
            argv.extend(("--ro-bind", str(path), str(path)))
        for root in self.allowed_directories:
            argv.extend(("--bind", str(root), str(root)))
        argv.extend(("--dir", "/tmp/enkiball-home"))
        argv.extend(("--chdir", str(self.cwd), "--", "/bin/sh", "-c", command))
        return argv

    @staticmethod
    def _runtime_paths() -> tuple[Path, ...]:
        candidates = (
            "/bin",
            "/sbin",
            "/usr",
            "/lib",
            "/lib64",
            "/etc/alternatives",
            "/etc/ca-certificates",
            "/etc/group",
            "/etc/hosts",
            "/etc/ld.so.cache",
            "/etc/ld.so.conf",
            "/etc/ld.so.conf.d",
            "/etc/localtime",
            "/etc/nsswitch.conf",
            "/etc/passwd",
            "/etc/resolv.conf",
            "/etc/ssl",
        )
        return tuple(Path(path) for path in candidates if Path(path).exists())

    def _sandbox_environment(self) -> dict[str, str]:
        env: dict[str, str] = {
            "HOME": "/tmp/enkiball-home",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", ""),
            "PATH": os.pathsep.join(
                (
                    str(self.cwd / ".venv" / "bin"),
                    "/usr/local/sbin",
                    "/usr/local/bin",
                    "/usr/sbin",
                    "/usr/bin",
                    "/sbin",
                    "/bin",
                )
            ),
            "TMPDIR": "/tmp",
        }
        return {key: value for key, value in env.items() if value}
