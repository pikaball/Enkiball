from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Literal


class PermissionDeniedError(PermissionError):
    pass


class SandboxUnavailableError(RuntimeError):
    pass


class TurnInterrupted(Exception):
    pass


ApprovalDecision = Literal["once", "always", "deny"]
ApprovalHandler = Callable[[Path, str], ApprovalDecision | bool]
MAX_OUTPUT_BYTES = 1_000_000


class Sandbox:
    def __init__(
        self,
        cwd: str | Path | None = None,
        dir_permissions: Iterable[str | Path] | None = None,
        approval: bool = False,
        sandbox: bool = True,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        if not self.cwd.is_dir():
            raise NotADirectoryError(f"Working directory not found: {self.cwd}")
        self.approval = bool(approval)
        self.enabled = bool(sandbox)
        self._write_roots = {self.cwd}
        self._read_roots: set[Path] = set()
        self._roots_lock = threading.RLock()
        self._approval_handler: ApprovalHandler | None = None
        for path in dir_permissions or ():
            self._write_roots.add(self._resolve_root(path))

    @property
    def allowed_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._write_roots | self._read_roots, key=str))

    @property
    def writable_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._write_roots, key=str))

    @property
    def read_only_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._read_roots - self._write_roots, key=str))

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def resolve_path(self, path: str | Path, operation: str) -> Path:
        raw = Path(path).expanduser()
        target = raw if raw.is_absolute() else self.cwd / raw
        target = target.resolve(strict=False)
        write_operation = operation in {"write", "edit"}
        if self._is_allowed(target, write=write_operation):
            return target

        requested_root = target if target.is_dir() else target.parent
        decision: ApprovalDecision | bool = "deny"
        if self.approval and self._approval_handler is not None:
            decision = self._approval_handler(requested_root, operation)
        if decision is True:
            decision = "always"
        elif decision is False:
            decision = "deny"
        if decision == "always":
            with self._roots_lock:
                if write_operation:
                    self._write_roots.add(requested_root)
                    self._read_roots.discard(requested_root)
                else:
                    self._read_roots.add(requested_root)
            return target
        if decision == "once":
            return target
        raise PermissionDeniedError(
            f"{operation} denied outside allowed directories: {target}"
        )

    def run(
        self,
        command: str,
        timeout_sec: int = 120,
        privileged: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, str, str]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if isinstance(timeout_sec, bool) or not 1 <= timeout_sec <= 600:
            raise ValueError("timeout_sec must be between 1 and 600")
        if not isinstance(privileged, bool):
            raise TypeError("privileged must be a boolean")

        run_on_host = not self.enabled or privileged
        if privileged and self.enabled:
            decision: ApprovalDecision | bool = "deny"
            if self.approval and self._approval_handler is not None:
                decision = self._approval_handler(self.cwd, "privileged command")
            if decision not in (True, "once"):
                raise PermissionDeniedError("privileged command denied")

        argv = (
            ["/bin/sh", "-c", command]
            if run_on_host
            else self._build_bwrap_command(command)
        )
        process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=os.environ.copy() if run_on_host else self._sandbox_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stdout_thread = self._drain_output(process.stdout, stdout_parts)
        stderr_thread = self._drain_output(process.stderr, stderr_parts)
        deadline = time.monotonic() + timeout_sec
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.1):
                    self._kill_process_group(process)
                    raise TurnInterrupted
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_process_group(process)
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_sec,
                        self._decode_output(stdout_parts),
                        self._decode_output(stderr_parts),
                    ) from None
                if cancel_event is None:
                    try:
                        process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
        except BaseException:
            if process.poll() is None:
                self._kill_process_group(process)
            raise
        finally:
            stdout_thread.join()
            stderr_thread.join()
        return (
            process.returncode,
            self._decode_output(stdout_parts),
            self._decode_output(stderr_parts),
        )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

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

    def _is_allowed(self, target: Path, *, write: bool = False) -> bool:
        with self._roots_lock:
            roots = self._write_roots if write else self._write_roots | self._read_roots
            return any(
                target == root or target.is_relative_to(root) for root in roots
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
        for root in self.read_only_directories:
            argv.extend(("--ro-bind", str(root), str(root)))
        for root in self.writable_directories:
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
