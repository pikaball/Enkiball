"""Authenticated local backend for persistent sandbox budgets."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import pwd
import select
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .bpf import CgroupPolicy
from .isolation import Mounts, base_policy
from .policy import fields, validate_policy
from .resources import ResourceGroup, remove_group

VERSION = 1
MAX_REQUEST = 48 * 1024 * 1024
MAX_OUTPUT = 1_000_000


def valid_id(value):
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Invalid instance or execution ID")
    return value


def environment(value):
    if not isinstance(value, dict):
        raise TypeError("env must be an object")
    if any(
        not isinstance(k, str)
        or not k
        or "=" in k
        or "\0" in k
        or not isinstance(v, str)
        or "\0" in v
        for k, v in value.items()
    ):
        raise ValueError("Invalid environment")
    return value


class Execution:
    def __init__(
        self, instance, execution_id, command, cwd, env, timeout, stdin, output_limit
    ):
        self.instance = instance
        self.id = execution_id
        self.state = "running"
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.result = None
        self.oom_before = self.oom_count()
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.stdin = stdin
        self.output_limit = output_limit
        self.process = None
        self.group = instance.group.task(execution_id)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def oom_count(self):
        path = self.instance.group.path / "memory.events"
        if not path.exists():
            return 0
        return int(
            dict(line.split() for line in path.read_text().splitlines()).get(
                "oom_kill", "0"
            )
        )

    def cancel(self):
        self.cancelled.set()
        # The worker owns process startup and cleanup, avoiding PID/cgroup races.
        self.done.wait(5)
        if not self.done.is_set():
            raise RuntimeError("Task cancellation did not complete")
        return self.snapshot()

    def snapshot(self):
        return {
            "execution_id": self.id,
            "state": self.state if self.done.is_set() else "running",
            **(self.result or {}),
        }

    def run(self):
        out, err = bytearray(), bytearray()
        reason = None
        try:
            argv = self.instance.mounts.argv(
                self.command, self.cwd, self.instance.directory / "tmp"
            )
            spec = {
                "uid": self.instance.uid,
                "gid": self.instance.gid,
                "groups": self.instance.groups,
                "cgroup": str(self.group),
                "argv": argv,
                "env": self.env,
            }
            with (
                tempfile.TemporaryFile() as data,
                tempfile.TemporaryFile() as input_file,
                tempfile.TemporaryFile() as seccomp_data,
            ):
                fds = [*self.instance.mounts.fds, data.fileno()]
                if self.instance.mounts.seccomp:
                    original = self.instance.mounts.seccomp.fileno()
                    seccomp_data.write(
                        os.pread(original, os.fstat(original).st_size, 0)
                    )
                    seccomp_data.seek(0)
                    spec["argv"][spec["argv"].index("--seccomp") + 1] = str(
                        seccomp_data.fileno()
                    )
                    fds.append(seccomp_data.fileno())
                data.write(json.dumps(spec).encode())
                data.seek(0)
                if self.stdin is not None:
                    input_file.write(self.stdin.encode())
                    input_file.seek(0)
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("launcher.py")),
                        str(data.fileno()),
                    ],
                    pass_fds=tuple(fds),
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            streams = {self.process.stdout: out, self.process.stderr: err}
            deadline = time.monotonic() + self.timeout
            while streams or self.process.poll() is None:
                if self.cancelled.is_set():
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                self.instance.check_interfaces()
                ready, _, _ = select.select(list(streams), [], [], 0.05)
                for stream in ready:
                    chunk = os.read(stream.fileno(), 65536)
                    if chunk:
                        remaining = self.output_limit + 1 - len(streams[stream])
                        streams[stream].extend(chunk[: max(remaining, 0)])
                    else:
                        streams.pop(stream)
            self.state = reason or "exited"
        except Exception as error:
            self.state = "failed"
            self.result = {"error": str(error)}
        finally:
            try:
                # Reaps daemonized descendants as well as the shell.
                remove_group(self.group)
                if self.process:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait()
                    for stream in (self.process.stdout, self.process.stderr):
                        stream.close()
            except Exception as error:
                self.state = "failed"
                self.result = {"error": f"Task cleanup failed: {error}"}

            def decode(content):
                return content[: self.output_limit].decode(
                    "utf-8", errors="replace"
                ) + ("\n...[truncated]" if len(content) > self.output_limit else "")

            self.result = {
                **(self.result or {}),
                "exit_code": self.process.returncode if self.process else None,
                "stdout": decode(out),
                "stderr": decode(err),
                "oom_killed": self.oom_count() > self.oom_before,
            }
            self.done.set()


class Instance:
    def __init__(self, backend, uid, gid, policy):
        self.id = uuid.uuid4().hex
        self.uid, self.gid = uid, gid
        account = pwd.getpwuid(uid)
        self.groups = os.getgrouplist(account.pw_name, gid)
        self.policy = policy
        self.directory = backend.state / self.id
        self.directory.mkdir(mode=0o711)
        self.group = self.mounts = self.filters = None
        self.tasks = {}
        self.lock = threading.RLock()
        self.state = "active"
        self.interfaces = {}
        try:
            self.interfaces = (
                {
                    r["interface"]: socket.if_nametoindex(r["interface"])
                    for r in policy["network"].get("outbound", [])
                    if "interface" in r
                }
                if isinstance(policy["network"], dict)
                else {}
            )
            temporary = self.directory / "tmp"
            temporary.mkdir(mode=0o700)
            (temporary / "enkiball-home").mkdir()
            if os.geteuid() == 0:
                os.chown(temporary, uid, gid)
                os.chown(temporary / "enkiball-home", uid, gid)
            self.group = ResourceGroup(backend.cgroup, self.id, policy["resources"])
            self.mounts = Mounts(policy, self.directory, uid, gid, self.groups)
            network = (
                policy["network"]
                if isinstance(policy["network"], dict) and policy["network"]["outbound"]
                else None
            )
            # All explicit devices are checked; aliases with conflicting direction fail.
            if network or policy["devices"]:
                self.filters = CgroupPolicy(self.group.path, network, policy["devices"])
        except BaseException:
            self.close()
            raise

    def check_interfaces(self):
        for name, index in self.interfaces.items():
            if socket.if_nametoindex(name) != index:
                raise RuntimeError(
                    f"Network interface changed; recreate sandbox: {name}"
                )

    def snapshot(self):
        policy = copy.deepcopy(self.policy)
        policy["filesystem"] = copy.deepcopy(self.mounts.effective_filesystem)
        return {
            "sandbox_id": self.id,
            "state": self.state,
            "execution_mode": "sandbox",
            "effective_policy": policy,
            "base_filesystem": base_policy(),
            "capability_scope": "sandbox user namespace",
            "temporary_directory": "instance",
            "runtime_mounts": [
                {"path": "/tmp", "access": "write", "scope": "instance"},
                {"path": "/proc", "type": "proc", "scope": "task"},
                {"path": "/dev", "type": "minimal-dev", "scope": "task"},
            ],
            "swap": "disabled"
            if "memory_bytes" in self.policy["resources"]
            else "inherited",
            "oom_scope": "instance",
            "network_namespace": "task" if self.mounts.isolated_network else "host",
            "network_interfaces": dict(self.interfaces),
            "resources": self.group.inspect(),
            "executions": [
                t.snapshot() | {"stdout": "", "stderr": ""} for t in self.tasks.values()
            ],
        }

    def execute(self, request):
        command, cwd = request.get("command"), request.get("cwd")
        timeout = request.get("timeout_sec", 120)
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("command must be a non-empty string without NUL")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or "\0" in cwd:
            raise ValueError("cwd must be an absolute path")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("timeout_sec must be between 1 and 600")
        env = environment(request.get("env", {}))
        stdin = request.get("stdin")
        if stdin is not None and (
            not isinstance(stdin, str) or len(stdin.encode()) > 1024**2
        ):
            raise ValueError("stdin must be a string of at most 1 MiB")
        output_limit = request.get("output_limit", MAX_OUTPUT)
        if type(output_limit) is not int or not 1 <= output_limit <= 32 * 1024**2:
            raise ValueError("output_limit must be between 1 byte and 32 MiB")
        with self.lock:
            if self.state != "active":
                raise RuntimeError("Sandbox is closing")
            self.check_interfaces()
            identity = valid_id(request.get("execution_id", uuid.uuid4().hex))
            if identity in self.tasks:
                raise ValueError("Execution ID already exists")
            # Bound retained output. Running tasks are never evicted.
            finished = [key for key, task in self.tasks.items() if task.done.is_set()]
            retained = sum(
                len((t.result or {}).get("stdout", "").encode())
                + len((t.result or {}).get("stderr", "").encode())
                for t in self.tasks.values()
            )
            while (len(self.tasks) >= 128 or retained > 64 * 1024**2) and finished:
                removed = self.tasks.pop(finished.pop(0))
                retained -= len(
                    (removed.result or {}).get("stdout", "").encode()
                ) + len((removed.result or {}).get("stderr", "").encode())
            if len(self.tasks) >= 128:
                raise RuntimeError("Sandbox task limit reached")
            task = Execution(
                self, identity, command, cwd, env, timeout, stdin, output_limit
            )
            self.tasks[identity] = task
            task.thread.start()
            return {"execution_id": identity, "state": "running"}

    def close(self):
        with self.lock:
            self.state = "closing"
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancelled.set()
        for task in tasks:
            if not task.done.wait(10):
                raise RuntimeError("Task did not stop during sandbox destruction")
        if self.group:
            self.group.close()
        if self.filters:
            self.filters.close()
        if self.mounts:
            self.mounts.close()
        # Caller-owned temporary trees may contain symlinks; rmtree does not follow them.
        shutil.rmtree(self.directory)
        self.state = "destroyed"


class Backend:
    def __init__(self, state, cgroup, authorization=None):
        self.state, self.cgroup = Path(state), Path(cgroup)
        self.state.mkdir(mode=0o711, parents=True, exist_ok=True)
        if (
            self.state.stat().st_uid != os.geteuid()
            or self.state.stat().st_mode & 0o022
        ):
            raise PermissionError(
                "Backend state directory must be owned by its service user and not writable by others"
            )
        self.lockfile = (self.state / "backend.lock").open("a")
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.instances = {}
        self.lock = threading.RLock()
        self.authorization = authorization or {}
        # A restart invalidates old IDs and kills all previously owned tasks.
        for old in self.cgroup.glob("sandbox-*"):
            remove_group(old)
        for old in self.state.iterdir():
            if old.is_dir():
                valid_id(old.name)
                shutil.rmtree(old)

    def authorize(self, uid, policy):
        # An unprivileged service cannot grant more authority than its own identity.
        if uid == os.geteuid():
            return
        limits = self.authorization.get(str(uid))
        if not isinstance(limits, dict):
            raise PermissionError("Caller is not authorized to create sandboxes")
        for rule in policy["filesystem"]:
            if rule["access"] == "deny":
                continue
            roots = limits.get(
                "write_paths" if rule["access"] == "write" else "read_paths", []
            )
            if rule["access"] == "read":
                roots = [*roots, *limits.get("write_paths", [])]
            if not any(
                Path(rule["path"]).is_relative_to(Path(p).resolve()) for p in roots
            ):
                raise PermissionError(f"Path is not authorized: {rule['path']}")
        for rule in policy["devices"]:
            if rule not in limits.get("devices", []):
                raise PermissionError("Device access is not authorized")
        if set(policy["capabilities"]) - set(limits.get("capabilities", [])):
            raise PermissionError("Capabilities are not authorized")
        # Administrators authorize complete paired network rules, never Cartesian products.
        if policy["network"] != {"outbound": []} and policy[
            "network"
        ] not in limits.get("networks", []):
            raise PermissionError("Network policy is not authorized")
        for key, maximum in limits.get("resources", {}).items():
            if (
                key not in ("memory_bytes", "cpu_count")
                or type(maximum) is not int
                or maximum < 1
            ):
                raise ValueError("Invalid administrator resource ceiling")
            if key not in policy["resources"] or policy["resources"][key] > maximum:
                raise PermissionError(f"Resource ceiling required: {key} <= {maximum}")

    def request(self, uid, gid, request):
        if (
            not isinstance(request, dict)
            or type(request.get("version")) is not int
            or request["version"] != VERSION
        ):
            raise ValueError("Unsupported protocol version; expected 1")
        op = request.get("op")
        operations = {
            "create": {"policy"},
            "inspect": {"sandbox_id"},
            "destroy": {"sandbox_id"},
            "exec": {
                "sandbox_id",
                "execution_id",
                "command",
                "cwd",
                "env",
                "timeout_sec",
                "stdin",
                "output_limit",
            },
            "wait": {"sandbox_id", "execution_id"},
            "cancel": {"sandbox_id", "execution_id"},
        }
        if not isinstance(op, str) or op not in operations:
            raise ValueError("Unknown operation")
        fields(request, {"version", "op"} | operations[op], "request")
        if op == "create":
            raw = request.get("policy")
            if not isinstance(raw, dict):
                raise TypeError("policy must be an object")
            for name in ("filesystem", "devices"):
                if any(
                    not isinstance(r, dict)
                    or not isinstance(r.get("path"), str)
                    or not Path(r["path"]).is_absolute()
                    for r in raw.get(name, [])
                ):
                    raise ValueError("Backend policy paths must be absolute")
            policy = validate_policy(raw, Path("/"))
            self.authorize(uid, policy)
            with self.lock:
                if len(self.instances) >= 128:
                    raise RuntimeError("Backend instance limit reached")
                instance = Instance(self, uid, gid, policy)
                self.instances[instance.id] = instance
            return instance.snapshot()
        identity = valid_id(request.get("sandbox_id"))
        with self.lock:
            instance = self.instances.get(identity)
        if instance is None:
            raise FileNotFoundError(
                "Sandbox does not exist; backend restart invalidates IDs"
            )
        if instance.uid != uid:
            raise PermissionError("Caller does not own this sandbox")
        if op == "destroy":
            with instance.lock:
                instance.close()
            with self.lock:
                self.instances.pop(identity, None)
            return {"sandbox_id": identity, "state": "destroyed"}
        with instance.lock:
            if instance.state != "active":
                raise RuntimeError("Sandbox is closing")
            if op == "inspect":
                return instance.snapshot()
            if op == "exec":
                return instance.execute(request)
            task = instance.tasks.get(valid_id(request.get("execution_id")))
            if task is None:
                raise FileNotFoundError(
                    "Execution does not exist or its result has expired"
                )
        if op == "cancel":
            return task.cancel()
        task.done.wait(0.25)
        return task.snapshot()

    def close(self):
        errors = []
        for instance in list(self.instances.values()):
            try:
                instance.close()
            except Exception as error:
                errors.append(str(error))
        self.lockfile.close()
        if errors:
            raise RuntimeError("; ".join(errors))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    request_queue_size = 64

    def __init__(self, path, backend):
        self.backend = backend
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(str(path), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            _, uid, gid = struct.unpack(
                "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            data = bytearray()
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_REQUEST:
                    raise ValueError("Request exceeds size limit")
            request = json.loads(data)
            result = self.server.backend.request(uid, gid, request)
            response = {"version": VERSION, "ok": True, "result": result}
        except Exception as error:
            response = {
                "version": VERSION,
                "ok": False,
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        try:
            self.request.sendall(
                json.dumps(response, ensure_ascii=False).encode() + b"\n"
            )
        except (OSError, TimeoutError):
            pass


def prepare_cgroup(root):
    if root is None:
        relative = next(
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    root = Path(root).resolve()
    if not (root / "cgroup.controllers").exists():
        raise RuntimeError("A delegated cgroup v2 root is required")
    manager = root / "backend"
    manager.mkdir(exist_ok=True)
    (manager / "cgroup.procs").write_text(str(os.getpid()))
    available = set((root / "cgroup.controllers").read_text().split()) & {
        "memory",
        "cpuset",
    }
    if available:
        (root / "cgroup.subtree_control").write_text(
            " ".join("+" + n for n in available)
        )
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/enkisandbox/control.sock")
    parser.add_argument("--state", default="/run/enkisandbox/state")
    parser.add_argument("--cgroup-root")
    parser.add_argument(
        "--authorization", help="root-owned JSON mapping UID to authority limits"
    )
    parser.add_argument(
        "--socket-group", type=int, help="numeric group allowed to connect"
    )
    args = parser.parse_args()
    authorization = None
    if args.authorization:
        p = Path(args.authorization)
        if p.stat().st_uid != os.geteuid() or p.stat().st_mode & 0o022:
            raise PermissionError(
                "Authorization file must be owned by the service user and not writable by others"
            )
        authorization = json.loads(p.read_text())
    backend = Backend(args.state, prepare_cgroup(args.cgroup_root), authorization)
    path = Path(args.socket)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = Server(path, backend)
    if args.socket_group is not None:
        os.chown(path, -1, args.socket_group)
    os.chmod(path, 0o660 if args.socket_group is not None else 0o600)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        backend.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
