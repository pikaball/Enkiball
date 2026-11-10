from __future__ import annotations

import copy
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Literal, Mapping

from .sandbox_policy import validate_policy


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
_UNSET = object()


class EnkiSandbox:
    """Immutable shared sandbox. Only a missing client enables host fallback.

    Relative policy paths are anchored to ``base_dir`` at construction. An
    omitted filesystem policy grants no working directory. Use ``host()`` for
    explicit host execution. Owners destroy on close; attached handles detach.
    """

    def __init__(
        self,
        *,
        filesystem=None,
        devices=None,
        network=None,
        capabilities=None,
        seccomp=None,
        resources=None,
        base_dir=None,
        client=None,
        socket_path=None,
    ):
        self._base_dir = Path(base_dir or Path.cwd()).expanduser().resolve()
        policy = {
            "filesystem": [] if filesystem is None else filesystem,
            "devices": [] if devices is None else devices,
            "network": {"outbound": []} if network is None else network,
            "capabilities": [] if capabilities is None else capabilities,
            "resources": {} if resources is None else resources,
        }
        if seccomp is not None:
            policy["seccomp"] = seccomp
        self._requested_policy = validate_policy(policy, self._base_dir)
        self._client = shutil.which(
            str(client or os.environ.get("ENKISANDBOX_CLIENT", "enkisandbox-client"))
        )
        self._socket_path = str(
            socket_path
            or os.environ.get("ENKISANDBOX_SOCKET", "/run/enkisandbox/control.sock")
        )
        self._owner = True
        self._closed = False
        self._id = None
        self._mode = "host"
        self._effective_policy = {}
        self._metadata = {}
        self._creator_pid = os.getpid()
        if self._client is not None:
            self._accept(self._rpc("create", policy=self._requested_policy))

    @classmethod
    def host(cls):
        """Explicitly run as the current host user without policy enforcement."""
        obj = cls.__new__(cls)
        obj._base_dir = Path.cwd()
        obj._client = None
        obj._socket_path = ""
        obj._owner = True
        obj._closed = False
        obj._id = None
        obj._mode = "host"
        obj._requested_policy = {}
        obj._effective_policy = {}
        obj._metadata = {}
        obj._creator_pid = os.getpid()
        return obj

    @classmethod
    def attach(cls, sandbox_id, *, client=None, socket_path=None):
        obj = cls.host()
        obj._client = shutil.which(
            str(client or os.environ.get("ENKISANDBOX_CLIENT", "enkisandbox-client"))
        )
        if obj._client is None:
            raise SandboxUnavailableError("Cannot attach without enkisandbox-client")
        obj._socket_path = str(
            socket_path
            or os.environ.get("ENKISANDBOX_SOCKET", "/run/enkisandbox/control.sock")
        )
        obj._owner = False
        obj._accept(obj._rpc("inspect", sandbox_id=sandbox_id))
        obj._requested_policy = obj.effective_policy
        return obj

    def _accept(self, result):
        if (
            result.get("execution_mode") != "sandbox"
            or not isinstance(result.get("sandbox_id"), str)
            or not isinstance(result.get("effective_policy"), dict)
        ):
            raise SandboxUnavailableError("Invalid backend sandbox response")
        self._id = result["sandbox_id"]
        self._mode = "sandbox"
        self._effective_policy = copy.deepcopy(result["effective_policy"])
        self._metadata = copy.deepcopy(result)

    def _rpc(self, op, **data):
        request = {"version": 1, "op": op, **data}
        try:
            result = subprocess.run(
                [self._client, "--socket", self._socket_path],
                input=json.dumps(request, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxUnavailableError(f"Sandbox client failed: {error}") from error
        if result.returncode:
            raise SandboxUnavailableError(
                f"Sandbox client exited {result.returncode}: {result.stderr.strip()}"
            )
        try:
            response = json.loads(result.stdout)
            if (
                type(response.get("version")) is not int
                or response["version"] != 1
                or type(response.get("ok")) is not bool
            ):
                raise SandboxUnavailableError("Unsupported response envelope")
            if not response["ok"]:
                error = response.get("error", {})
                kind = error.get("type")
                message = error.get("message", "Backend request failed")
                if kind == "PermissionError":
                    raise PermissionDeniedError(message)
                if kind in ("ValueError", "TypeError"):
                    raise ValueError(message)
                raise SandboxUnavailableError(message)
            if not isinstance(response.get("result"), dict):
                raise SandboxUnavailableError("Missing response result")
            return response["result"]
        except (json.JSONDecodeError, AttributeError) as error:
            raise SandboxUnavailableError("Invalid sandbox client response") from error

    @property
    def sandbox_id(self):
        return self._id

    @property
    def execution_mode(self):
        return self._mode

    @property
    def effective_policy(self):
        return copy.deepcopy(self._effective_policy)

    @property
    def requested_policy(self):
        return copy.deepcopy(self._requested_policy)

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Sandbox handle is closed")

    def inspect(self):
        self._check_open()
        if self._mode == "host":
            return {
                "sandbox_id": None,
                "execution_mode": "host",
                "state": "active",
                "effective_policy": {},
            }
        return self._rpc("inspect", sandbox_id=self._id)

    def cancel(self, execution_id):
        self._check_open()
        if self._mode != "sandbox":
            raise RuntimeError(
                "Host commands are cancelled through their cancellation event"
            )
        return self._rpc("cancel", sandbox_id=self._id, execution_id=execution_id)

    def destroy(self, *, allow_attached=False):
        """Destroy owned instances; attached handles need explicit authorization."""
        if self._closed:
            return
        if (not self._owner or self._creator_pid != os.getpid()) and not allow_attached:
            raise PermissionDeniedError(
                "Destroying an attached or inherited sandbox requires allow_attached=True"
            )
        if self._mode == "sandbox":
            self._rpc("destroy", sandbox_id=self._id)
        self._closed = True

    def close(self):
        if self._closed:
            return
        if self._owner and self._creator_pid == os.getpid():
            self.destroy()
        else:
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *exc):
        self.close()

    def run(
        self,
        command,
        *,
        cwd=None,
        env=None,
        timeout_sec=120,
        cancel_event=None,
        stdin=None,
        output_limit=MAX_OUTPUT_BYTES,
    ):
        self._check_open()
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("command must be a non-empty string without NUL")
        if type(timeout_sec) is not int or not 1 <= timeout_sec <= 600:
            raise ValueError("timeout_sec must be between 1 and 600")
        cwd = Path(cwd or self._base_dir).expanduser().absolute()
        if cancel_event is not None and cancel_event.is_set():
            raise TurnInterrupted
        env = Sandbox._validate_environment(env or {})
        if stdin is not None and (
            not isinstance(stdin, str) or len(stdin.encode()) > 1024**2
        ):
            raise ValueError("stdin must be a string of at most 1 MiB")
        if type(output_limit) is not int or not 1 <= output_limit <= 32 * 1024**2:
            raise ValueError("output_limit must be between 1 byte and 32 MiB")
        if self._mode == "host":
            host = Sandbox(cwd=cwd, sandbox=False)
            host._host_env = os.environ.copy() | env
            host._host_input = stdin
            host._output_limit = output_limit
            return host._run(command, timeout_sec, False, cancel_event)
        env = {
            "HOME": "/tmp/enkiball-home",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "PATH": str(cwd / ".venv" / "bin") + ":/usr/local/bin:/usr/bin:/bin",
            **env,
        }
        execution_id = uuid.uuid4().hex
        try:
            self._rpc(
                "exec",
                sandbox_id=self._id,
                execution_id=execution_id,
                command=command,
                cwd=str(cwd),
                env=env,
                timeout_sec=timeout_sec,
                stdin=stdin,
                output_limit=output_limit,
            )
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise TurnInterrupted
                result = self._rpc(
                    "wait", sandbox_id=self._id, execution_id=execution_id
                )
                state = result.get("state")
                if state == "running":
                    continue
                if state == "cancelled":
                    raise TurnInterrupted
                if state == "timeout":
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_sec,
                        result.get("stdout", ""),
                        result.get("stderr", ""),
                    )
                if state == "failed":
                    raise SandboxUnavailableError(
                        result.get("error", "Backend execution failed")
                    )
                if state != "exited" or type(result.get("exit_code")) is not int:
                    raise SandboxUnavailableError("Invalid backend execution result")
                return (
                    result["exit_code"],
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                )
        except BaseException:
            try:
                self._rpc("cancel", sandbox_id=self._id, execution_id=execution_id)
            except Exception:
                pass  # Backend deadlines still bound tasks when transport is lost.
            raise


class Sandbox:
    def __init__(
        self,
        cwd: str | Path | None = None,
        dir_permissions: Iterable[str | Path] | None = None,
        approval: bool = False,
        sandbox: bool | EnkiSandbox = True,
        network: SandboxNetwork = "none",
        env: Mapping[str, str] | None = None,
        inherit_env: Iterable[str] | None = None,
        ro_permissions: Iterable[str | Path] | None = None,
        policy: Mapping | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        if not self.cwd.is_dir():
            raise NotADirectoryError(f"Working directory not found: {self.cwd}")
        self._policy = validate_policy(
            {
                **(policy or {}),
                "network": (policy or {}).get(
                    "network", "all" if network == "host" else {"outbound": []}
                ),
            },
            self.cwd,
        )
        self._binding = sandbox if isinstance(sandbox, EnkiSandbox) else None
        self._runtime = self._binding
        self._runtime_key = None
        self.approval = bool(approval)
        self.enabled = sandbox is not False
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
        self._retired = []
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

    def set_managed_read_roots(self, owner: str, paths: Iterable[str | Path]) -> None:
        roots = {self._resolve_root(path) for path in paths}
        with self._roots_lock:
            self._managed_read_roots[owner] = roots

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def configure(
        self,
        *,
        policy: Mapping | None | object = _UNSET,
        enabled: bool | object = _UNSET,
        network: SandboxNetwork | object = _UNSET,
        env: Mapping[str, str] | None | object = _UNSET,
        inherit_env: Iterable[str] | None | object = _UNSET,
        dir_permissions: Iterable[str | Path] | None | object = _UNSET,
        ro_permissions: Iterable[str | Path] | None | object = _UNSET,
        approval: bool | object = _UNSET,
    ) -> None:
        """Compatibility API: replace the bound runtime for subsequent calls."""
        if self._binding is not None:
            raise ValueError(
                "EnkiSandbox policies are immutable; assign agent.sandbox to a new object"
            )
        if enabled is not _UNSET and not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        if approval is not _UNSET and not isinstance(approval, bool):
            raise TypeError("approval must be a boolean")
        if network is not _UNSET and network not in ("none", "host"):
            raise ValueError("network must be 'none' or 'host'")
        if dir_permissions is not _UNSET and dir_permissions is not None:
            dir_permissions = tuple(dir_permissions)
        if ro_permissions is not _UNSET and ro_permissions is not None:
            ro_permissions = tuple(ro_permissions)
        new_policy = validate_policy(policy, self.cwd) if policy is not _UNSET else None
        if network is not _UNSET and (
            policy is _UNSET or not (policy or {}).get("network")
        ):
            new_policy = new_policy or copy.deepcopy(self._policy)
            new_policy["network"] = "all" if network == "host" else {"outbound": []}
        new_env = self._validate_environment(env or {}) if env is not _UNSET else None
        new_inherit = (
            self._validate_environment_names(inherit_env or ())
            if inherit_env is not _UNSET
            else None
        )
        # Construct roots before changing the live configuration.
        new_write = (
            None
            if dir_permissions is _UNSET
            else {self.cwd, *(self._resolve_root(p) for p in (dir_permissions or ()))}
        )
        new_read = (
            None
            if ro_permissions is _UNSET
            else {self._resolve_root(p) for p in (ro_permissions or ())}
        )
        aliases = dict(self._root_aliases)
        if new_write is not None or new_read is not None:
            # Aliases for unchanged roots remain valid.
            for paths in (dir_permissions, ro_permissions):
                if paths is _UNSET or paths is None:
                    continue
                for path in paths:
                    raw = Path(path).expanduser()
                    raw = raw if raw.is_absolute() else self.cwd / raw
                    resolved = raw.resolve(strict=False)
                    if raw != resolved:
                        aliases[raw] = resolved
        with self._roots_lock:
            if new_policy is not None:
                self._policy = new_policy
            if enabled is not _UNSET:
                self.enabled = enabled
            if approval is not _UNSET:
                self.approval = approval
            if network is not _UNSET:
                self.network = network
            if new_env is not None:
                self._env = new_env
            if new_inherit is not None:
                self._inherit_env = new_inherit
            if new_write is not None:
                self._write_roots = new_write
            if new_read is not None:
                self._read_roots = new_read
            self._root_aliases = {
                a: p
                for a, p in aliases.items()
                if p in self._write_roots | self._read_roots
            }

    @property
    def runtime(self) -> EnkiSandbox:
        if self._binding is not None:
            return self._binding
        policy = copy.deepcopy(self._policy)
        roots = {
            str(p): {"path": str(p), "access": "write"}
            for p in self.writable_directories
        }
        roots.update(
            {
                str(p): {"path": str(p), "access": "read"}
                for p in self.read_only_directories
            }
        )
        roots.update({r["path"]: r for r in policy["filesystem"]})
        policy["filesystem"] = list(roots.values())
        key = json.dumps(policy, sort_keys=True) if self.enabled else "host"
        with self._roots_lock:
            if self._runtime is None or key != self._runtime_key:
                # Legacy reconfiguration creates a replacement. Keep previous budgets
                # alive until this adapter closes, so in-flight tasks remain valid.
                if self._runtime is not None:
                    self._retired.append(self._runtime)
                self._runtime = (
                    EnkiSandbox(**policy, base_dir=self.cwd)
                    if self.enabled
                    else EnkiSandbox.host()
                )
                self._runtime_key = key
            return self._runtime

    @property
    def command_context(self) -> dict[str, object]:
        runtime = self.runtime
        return {
            "cwd": str(self.cwd),
            "sandbox_id": runtime.sandbox_id,
            "execution_mode": runtime.execution_mode,
            "effective_policy": runtime.effective_policy,
            "policy": runtime.effective_policy,
            "policy_active": runtime.execution_mode == "sandbox",
            "network": runtime.effective_policy.get("network", "host"),
            "capability_scope": "sandbox user namespace"
            if runtime.execution_mode == "sandbox"
            else None,
            "runtime_mounts": copy.deepcopy(
                runtime._metadata.get("runtime_mounts", [])
            ),
            "network_namespace": runtime._metadata.get("network_namespace", "host"),
            "network_interfaces": copy.deepcopy(
                runtime._metadata.get("network_interfaces", {})
            ),
            "writable_directories": [
                r["path"]
                for r in runtime.effective_policy.get("filesystem", [])
                if r["access"] == "write"
            ],
            "read_only_directories": [
                r["path"]
                for r in runtime.effective_policy.get("filesystem", [])
                if r["access"] == "read"
            ],
            "privileged_approval_available": self._binding is None
            and self.enabled
            and self.approval
            and self._approval_handler is not None,
        }

    def close(self):
        if self._binding is None:
            for runtime in [*self._retired, self._runtime]:
                if runtime is not None:
                    runtime.close()
            self._retired.clear()

    def file_tool(self, name, args, cancel_event=None):
        from . import tools

        runtime = self.runtime
        if runtime.execution_mode == "sandbox":
            # Execute the same pure file operations inside the command boundary.
            # No SDK installation or import is needed in the sandbox.
            source = Path(tools.__file__).read_text(encoding="utf-8")
            source += "\nimport json,sys\na=json.loads(sys.stdin.read())\ntry:\n r=globals()[sys.argv[1]](**a); print(json.dumps({'ok':True,'result':r}))\nexcept Exception as e:\n print(json.dumps({'ok':False,'error':str(e)}))\n"
            command = (
                "/usr/bin/python3 -I -c "
                + shlex.quote(source)
                + " "
                + shlex.quote(name)
            )
            code, out, err = runtime.run(
                command,
                cwd=self.cwd,
                env=self._sandbox_environment(),
                cancel_event=cancel_event,
                stdin=json.dumps(args, ensure_ascii=False),
                output_limit=32 * 1024**2 if name == "read_image" else MAX_OUTPUT_BYTES,
            )
            if code:
                raise RuntimeError(err or f"File tool exited {code}")
            result = json.loads(out)
            if not result["ok"]:
                raise RuntimeError(result["error"])
            return result["result"]
        if cancel_event is not None and cancel_event.is_set():
            raise TurnInterrupted
        return getattr(tools, name)(**args)

    def resolve_path(self, path: str | Path, operation: str) -> Path:
        raw = Path(path).expanduser()
        target = raw if raw.is_absolute() else self.cwd / raw
        lexical_access = self._policy_access(Path(os.path.abspath(target)))
        target = target.resolve(strict=False)
        if self._binding is not None:
            self._binding._check_open()
            return Path(os.path.abspath(raw if raw.is_absolute() else self.cwd / raw))
        if lexical_access == "deny":
            raise PermissionDeniedError(f"{operation} denied by policy: {target}")
        write_operation = operation in {"write", "edit"}
        if self._is_allowed(target, write=write_operation):
            return target

        if self._policy_access(target) is not None:
            raise PermissionDeniedError(f"{operation} denied by policy: {target}")
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
        if not isinstance(privileged, bool):
            raise TypeError("privileged must be a boolean")
        runtime = self.runtime  # Capture the binding for the whole task.
        if (
            privileged
            and self._binding is not None
            and runtime.execution_mode == "sandbox"
        ):
            raise PermissionDeniedError("Host execution requires EnkiSandbox.host()")
        if privileged and self._binding is None:
            return self._run(command, timeout_sec, privileged, cancel_event)
        env = (
            self._sandbox_environment()
            if runtime.execution_mode == "sandbox"
            else self._env
        )
        return runtime.run(
            command,
            cwd=self.cwd,
            env=env,
            timeout_sec=timeout_sec,
            cancel_event=cancel_event,
        )

    def _run(
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

        if privileged and self.enabled:
            decision: ApprovalDecision | bool = "deny"
            if self.approval and self._approval_handler is not None:
                decision = self._approval_handler(self.cwd, "privileged command")
            if decision not in (True, "once"):
                raise PermissionDeniedError("privileged command denied")

        argv = ["/bin/sh", "-c", command]
        if cancel_event is not None and cancel_event.is_set():
            raise TurnInterrupted
        with tempfile.TemporaryFile() as input_file:
            if getattr(self, "_host_input", None) is not None:
                input_file.write(self._host_input.encode())
                input_file.seek(0)
            process = subprocess.Popen(
                argv,
                cwd=self.cwd,
                env=getattr(self, "_host_env", os.environ.copy()),
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stop_reading = threading.Event()
        stdout_thread = self._drain_output(
            process.stdout,
            stdout_parts,
            stop_reading,
            getattr(self, "_output_limit", MAX_OUTPUT_BYTES),
        )
        stderr_thread = self._drain_output(
            process.stderr,
            stderr_parts,
            stop_reading,
            getattr(self, "_output_limit", MAX_OUTPUT_BYTES),
        )
        deadline = time.monotonic() + timeout_sec
        try:
            # A shell may exit while its descendants still hold the pipes.
            # Keep enforcing cancellation/deadlines until output is drained.
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise TurnInterrupted
                if (
                    process.poll() is not None
                    and not stdout_thread.is_alive()
                    and not stderr_thread.is_alive()
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_sec,
                        self._decode_output(
                            stdout_parts,
                            getattr(self, "_output_limit", MAX_OUTPUT_BYTES),
                        ),
                        self._decode_output(
                            stderr_parts,
                            getattr(self, "_output_limit", MAX_OUTPUT_BYTES),
                        ),
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
            self._decode_output(
                stdout_parts, getattr(self, "_output_limit", MAX_OUTPUT_BYTES)
            ),
            self._decode_output(
                stderr_parts, getattr(self, "_output_limit", MAX_OUTPUT_BYTES)
            ),
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
        output_limit: int = MAX_OUTPUT_BYTES,
    ) -> threading.Thread:
        def _read() -> None:
            remaining = output_limit + 1
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
    def _decode_output(parts: list[bytes], output_limit: int = MAX_OUTPUT_BYTES) -> str:
        content = b"".join(parts)
        truncated = len(content) > output_limit
        decoded = content[:output_limit].decode("utf-8", errors="replace")
        return decoded + "\n...[truncated]" if truncated else decoded

    def _resolve_root(self, path: str | Path) -> Path:
        raw = Path(path).expanduser()
        root = raw if raw.is_absolute() else self.cwd / raw
        root = root.resolve(strict=False)
        if not root.is_dir():
            raise NotADirectoryError(f"Allowed directory not found: {root}")
        return root

    def _policy_access(self, target: Path) -> str | None:
        matches = [
            r
            for r in self._policy.get("filesystem", [])
            if target == Path(r["path"]) or target.is_relative_to(r["path"])
        ]
        return (
            max(matches, key=lambda r: len(Path(r["path"]).parts))["access"]
            if matches
            else None
        )

    def _is_allowed(self, target: Path, *, write: bool = False) -> bool:
        access = self._policy_access(target)
        if access is not None:
            return access == "write" or (access == "read" and not write)
        with self._roots_lock:
            read_only_roots = self._all_read_roots() - self._write_roots
            if write and any(
                target == root or target.is_relative_to(root)
                for root in read_only_roots
            ):
                return False
            roots = self._write_roots if write else self._write_roots | read_only_roots
            return any(target == root or target.is_relative_to(root) for root in roots)

    def _all_read_roots(self) -> set[Path]:
        managed = (
            set().union(*self._managed_read_roots.values())
            if self._managed_read_roots
            else set()
        )
        return self._read_roots | managed

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
