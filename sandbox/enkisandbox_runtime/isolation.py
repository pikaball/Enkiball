"""Bubblewrap mount snapshots and policy preparation."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .policy import compile_seccomp

RUNTIME_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
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


def base_policy():
    paths = {Path(p).resolve() for p in RUNTIME_PATHS if Path(p).exists()}
    return [
        {"path": str(p), "access": "read"}
        for p in sorted(paths, key=str)
        if not any(p != parent and p.is_relative_to(parent) for parent in paths)
    ]


def reject_unsafe_aliases(rules):
    """Refuse restrictive mounts that could leave accessible inode aliases."""
    writes = [Path(r["path"]) for r in rules if r["access"] == "write"]
    protected = [
        Path(r["path"])
        for r in rules
        if r["access"] == "deny"
        or (
            r["access"] == "read"
            and any(Path(r["path"]).is_relative_to(p) for p in writes)
        )
    ]
    # A hard link can otherwise bypass a path mask or read-only bind.
    for path in protected:
        pending = [path]
        while pending:
            current = pending.pop()
            info = current.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError(
                    f"Restrictive paths cannot contain hard-linked files: {current}"
                )
            if stat.S_ISDIR(info.st_mode):
                pending.extend(current.iterdir())
    # Resolve mount aliases to filesystem-relative names, including bind mounts.
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        parts = line.split()

        def unescape(s):
            for a, b in [
                ("\\040", " "),
                ("\\011", "\t"),
                ("\\012", "\n"),
                ("\\134", "\\"),
            ]:
                s = s.replace(a, b)
            return s

        mounts.append((Path(unescape(parts[4])), parts[2], Path(unescape(parts[3]))))

    def location(path):
        mount, device, root = max(
            (m for m in mounts if path.is_relative_to(m[0])),
            key=lambda m: len(m[0].parts),
        )
        return device, root / path.relative_to(mount)

    for path in protected:
        device, source = location(path)
        for mount, other_device, root in mounts:
            if device != other_device or not source.is_relative_to(root):
                continue
            alias = mount / source.relative_to(root)
            if alias == path:
                continue
            allowed = [r for r in rules if alias.is_relative_to(r["path"])]
            if (
                allowed
                and max(allowed, key=lambda r: len(Path(r["path"]).parts))["access"]
                != "deny"
            ):
                raise ValueError(
                    f"Restrictive path has a mount alias: {path} -> {alias}"
                )


class Mounts:
    def __init__(self, policy, directory: Path, uid, gid, groups):
        self.fds = []
        self.seccomp = None
        self.arguments = []
        self.bwrap = shutil.which("bwrap")
        if not self.bwrap:
            raise RuntimeError("Install bubblewrap on the backend host")
        caps = set(policy["capabilities"])
        # These capabilities can break mounts, process identity or BPF boundaries.
        safe = {"NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE", "IPC_LOCK"}
        if caps - safe:
            raise ValueError(
                f"Unsupported user namespace capabilities: {sorted(caps - safe)}"
            )
        if caps & {"NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE"} and (
            policy["network"] == "all" or policy["network"]["outbound"]
        ):
            raise ValueError(
                "Network capabilities require an isolated network namespace"
            )
        self.isolated_network = (
            policy["network"] != "all" and not policy["network"]["outbound"]
        )
        self.rules = policy["filesystem"]
        try:
            for rule in self.rules:
                p = Path(rule["path"])
                if not p.exists():
                    if rule["access"] != "write" or "type" not in rule:
                        raise ValueError(
                            f"Missing paths need write access and an explicit type: {p}"
                        )
                    code = (
                        "import os,sys; os.makedirs(sys.argv[1],exist_ok=True)"
                        if rule["type"] == "directory"
                        else "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.touch(exist_ok=True)"
                    )
                    identity = (
                        {"user": uid, "group": gid, "extra_groups": groups}
                        if os.geteuid() == 0
                        else {}
                    )
                    subprocess.run(
                        [sys.executable, "-I", "-c", code, str(p)],
                        check=True,
                        **identity,
                    )
                if (
                    p.resolve() != p
                    or p == Path("/")
                    or any(p.is_relative_to(x) for x in ("/proc", "/sys", "/dev"))
                    or p.is_relative_to(directory.parent.parent)
                    or directory.parent.parent.is_relative_to(p)
                    or any(
                        p == Path(x) or Path(x).is_relative_to(p)
                        for x in ("/proc", "/dev", "/tmp", "/sys")
                    )
                ):
                    raise ValueError(f"Reserved or changed mount path: {p}")
                if "type" in rule and p.is_dir() != (rule["type"] == "directory"):
                    raise ValueError(f"Path type does not match policy: {p}")
                if not (p.is_dir() or p.is_file()):
                    raise ValueError(
                        f"Filesystem rules require regular files or directories: {p}"
                    )
            reject_unsafe_aliases(self.rules)
            # Explicit rules override the visible base policy at the same path.
            combined = {
                r["path"]: r
                for r in base_policy()
                if not any(
                    Path(r["path"]).is_relative_to(custom["path"])
                    for custom in self.rules
                )
            }
            combined.update({r["path"]: r for r in self.rules})
            self.effective_filesystem = sorted(
                combined.values(), key=lambda r: (len(Path(r["path"]).parts), r["path"])
            )
            denied = []
            for rule in self.effective_filesystem:
                path, access = rule["path"], rule["access"]
                if access == "deny":
                    if Path(path).is_dir():
                        nested = any(
                            r["access"] != "deny"
                            and r["path"] != path
                            and Path(r["path"]).is_relative_to(path)
                            for r in self.rules
                        )
                        self.arguments += [
                            "--perms",
                            "111" if nested else "000",
                            "--tmpfs",
                            path,
                        ]
                        denied.append(path)
                    else:
                        mask = directory / ("mask-" + str(len(self.fds)))
                        mask.touch(mode=0)
                        self.arguments += ["--ro-bind", self.pin(mask), path]
                else:
                    self.arguments += [
                        "--bind" if access == "write" else "--ro-bind",
                        self.pin(Path(path)),
                        path,
                    ]
            for alias in RUNTIME_PATHS:
                path = Path(alias)
                if path.is_symlink():
                    self.arguments += ["--symlink", str(path.resolve()), alias]
            for path in denied:
                self.arguments += ["--remount-ro", path]
            for rule in policy["devices"]:
                info = Path(rule["path"]).stat()
                if not (stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)):
                    raise ValueError("Device rules require device nodes")
                self.arguments += [
                    "--dev-bind",
                    self.pin(Path(rule["path"])),
                    rule["path"],
                ]
            if "seccomp" in policy:
                self.seccomp = tempfile.TemporaryFile()
                compile_seccomp(policy["seccomp"], self.seccomp)
                self.arguments += ["--seccomp", str(self.seccomp.fileno())]
                self.fds.append(self.seccomp.fileno())
            self.capabilities = list(caps)
        except BaseException:
            self.close()
            raise

    def pin(self, path):
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        if stat.S_ISLNK(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"Mount source changed to a symlink: {path}")
        if Path(os.readlink(f"/proc/self/fd/{fd}")) != path:
            os.close(fd)
            raise ValueError(f"Mount source changed during policy creation: {path}")
        self.fds.append(fd)
        return f"/proc/self/fd/{fd}"

    def argv(self, command, cwd, temporary):
        reject_unsafe_aliases(self.rules)
        argv = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--cap-drop",
            "ALL",
        ]
        if self.isolated_network:
            argv.append("--unshare-net")
        argv += [
            "--tmpfs",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--bind",
            str(temporary),
            "/tmp",
        ]
        argv += self.arguments
        for cap in self.capabilities:
            argv += ["--cap-add", "CAP_" + cap]
        return argv + [
            "--remount-ro",
            "/",
            "--chdir",
            cwd,
            "--",
            "/bin/sh",
            "-c",
            command,
        ]

    def close(self):
        if self.seccomp:
            fd = self.seccomp.fileno()
            self.seccomp.close()
            if fd in self.fds:
                self.fds.remove(fd)
            self.seccomp = None
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()
