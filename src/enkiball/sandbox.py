from __future__ import annotations

import os
import platform
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Literal, Mapping


class PermissionDeniedError(PermissionError):
    pass


class SandboxUnavailableError(RuntimeError):
    pass


class TurnInterrupted(Exception):
    pass


ApprovalDecision = Literal["once", "always", "deny"]
SandboxNetwork = Literal["none", "host"]
ApprovalHandler = Callable[[Path, str], ApprovalDecision | bool]
MAX_OUTPUT_BYTES = 1_000_000


class Sandbox:
    def __init__(
        self,
        cwd: str | Path | None = None,
        dir_permissions: Iterable[str | Path] | None = None,
        approval: bool = False,
        sandbox: bool = True,
        network: SandboxNetwork = "none",
        env: Mapping[str, str] | None = None,
        inherit_env: Iterable[str] | None = None,
        ro_permissions: Iterable[str | Path] | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        if not self.cwd.is_dir():
            raise NotADirectoryError(f"Working directory not found: {self.cwd}")
        self.approval = bool(approval)
        self.enabled = bool(sandbox)
        if network not in {"none", "host"}:
            raise ValueError("network must be 'none' or 'host'")
        self.network: SandboxNetwork = network
        self._env = self._validate_environment(env or {})
        self._inherit_env = self._validate_environment_names(inherit_env or ())
        self._write_roots = {self.cwd}
        self._read_roots: set[Path] = set()
        self._managed_read_roots: dict[str, set[Path]] = {}
        self._roots_lock = threading.RLock()
        self._approval_handler: ApprovalHandler | None = None
        self._root_aliases: dict[Path, Path] = {}
        for path in dir_permissions or ():
            resolved = self._resolve_root(path)
            self._write_roots.add(resolved)
            raw = Path(path).expanduser()
            raw = raw if raw.is_absolute() else self.cwd / raw
            if raw != resolved:
                self._root_aliases[raw] = resolved
        for path in ro_permissions or ():
            resolved = self._resolve_root(path)
            self._read_roots.add(resolved)
            raw = Path(path).expanduser()
            raw = raw if raw.is_absolute() else self.cwd / raw
            if raw != resolved:
                self._root_aliases[raw] = resolved

    @property
    def allowed_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._write_roots | self._all_read_roots(), key=str))

    @property
    def writable_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._write_roots, key=str))

    @property
    def read_only_directories(self) -> tuple[Path, ...]:
        with self._roots_lock:
            return tuple(sorted(self._all_read_roots() - self._write_roots, key=str))

    def set_managed_read_roots(
        self, owner: str, paths: Iterable[str | Path]
    ) -> None:
        roots = {self._resolve_root(path) for path in paths}
        with self._roots_lock:
            self._managed_read_roots[owner] = roots

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    @property
    def command_context(self) -> dict[str, object]:
        """Describe current command permissions without exposing environment values."""
        return {
            "cwd": str(self.cwd),
            "execution_mode": "sandbox" if self.enabled else "host",
            "network": self.network if self.enabled else "host",
            "writable_directories": [str(p) for p in self.writable_directories],
            "read_only_directories": [str(p) for p in self.read_only_directories],
            "privileged_approval_available": (
                self.enabled and self.approval and self._approval_handler is not None
            ),
        }

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
        if cancel_event is not None and cancel_event.is_set():
            raise TurnInterrupted

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
        if cancel_event is not None and cancel_event.is_set():
            raise TurnInterrupted
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
        stop_reading = threading.Event()
        stdout_thread = self._drain_output(process.stdout, stdout_parts, stop_reading)
        stderr_thread = self._drain_output(process.stderr, stderr_parts, stop_reading)
        deadline = time.monotonic() + timeout_sec
        try:
            # A shell may exit while its descendants still hold the pipes.
            # Keep enforcing cancellation/deadlines until output is drained.
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise TurnInterrupted
                if process.poll() is not None and not stdout_thread.is_alive() and not stderr_thread.is_alive():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_sec,
                        self._decode_output(stdout_parts),
                        self._decode_output(stderr_parts),
                    ) from None
                if cancel_event is not None:
                    cancel_event.wait(min(0.05, remaining))
                else:
                    time.sleep(min(0.05, remaining))
        except BaseException:
            # Descendants can outlive the group leader; kill the owned group
            # even when the original shell has already been reaped.
            self._kill_process_group(process)
            raise
        finally:
            stop_reading.set()
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
        stop_reading: threading.Event,
    ) -> threading.Thread:
        def _read() -> None:
            remaining = MAX_OUTPUT_BYTES + 1
            try:
                while not stop_reading.is_set():
                    if not select.select([stream], [], [], 0.05)[0]:
                        continue
                    chunk = os.read(stream.fileno(), 65536)
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
            read_only_roots = self._all_read_roots() - self._write_roots
            if write and any(
                target == root or target.is_relative_to(root)
                for root in read_only_roots
            ):
                return False
            roots = self._write_roots if write else self._write_roots | read_only_roots
            return any(
                target == root or target.is_relative_to(root) for root in roots
            )

    def _all_read_roots(self) -> set[Path]:
        managed = (
            set().union(*self._managed_read_roots.values())
            if self._managed_read_roots
            else set()
        )
        return self._read_roots | managed

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
        ]
        if self.network == "none":
            argv.append("--unshare-net")
        argv.extend(
            [
                "--tmpfs",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
            ]
        )
        for root in self.writable_directories:
            argv.extend(("--bind", str(root), str(root)))
        for path in self._runtime_paths():
            argv.extend(("--ro-bind", str(path), str(path)))
        for root in self.read_only_directories:
            argv.extend(("--ro-bind", str(root), str(root)))
        # Editable installations may retain the configured symlink path in scripts.
        for alias, target in self._root_aliases.items():
            if not any(alias.is_relative_to(root) for root in self.allowed_directories):
                argv.extend(("--dir", str(alias.parent), "--symlink", str(target), str(alias)))
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
            "/etc/ssl"
        )
        return tuple(Path(path) for path in candidates if Path(path).exists())

    @staticmethod
    def _validate_environment(env: Mapping[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not key or "=" in key or "\0" in key:
                raise ValueError(f"invalid environment variable name: {key!r}")
            if not isinstance(value, str):
                raise TypeError(f"environment variable {key!r} must be a string")
            if "\0" in value:
                raise ValueError(f"environment variable {key!r} contains NUL")
            validated[key] = value
        return validated

    @staticmethod
    def _validate_environment_names(names: Iterable[str]) -> tuple[str, ...]:
        validated: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name or "=" in name or "\0" in name:
                raise ValueError(f"invalid environment variable name: {name!r}")
            validated.append(name)
        return tuple(dict.fromkeys(validated))

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
        for key in self._inherit_env:
            if key not in os.environ:
                raise ValueError(f"environment variable is not set: {key}")
            env[key] = os.environ[key]
        env.update(self._env)
        return {key: value for key, value in env.items() if value}
