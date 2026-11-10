"""Portable policy validation. System enforcement belongs to the backend."""

from __future__ import annotations

import copy
import ctypes
import ctypes.util
import errno
import ipaddress
import os
import re
from pathlib import Path
from typing import Mapping

CAPABILITIES = set(
    "CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID SETPCAP LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN NET_RAW IPC_LOCK IPC_OWNER SYS_MODULE SYS_RAWIO SYS_CHROOT SYS_PTRACE SYS_PACCT SYS_ADMIN SYS_BOOT SYS_NICE SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG MKNOD LEASE AUDIT_WRITE AUDIT_CONTROL SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG WAKE_ALARM BLOCK_SUSPEND AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE".split()
)


def fields(value, allowed, label):
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown {label} fields: {sorted(unknown)}")


def validate_policy(value, cwd: Path) -> dict:
    value = copy.deepcopy({} if value is None else dict(value))
    fields(
        value,
        ("filesystem", "devices", "capabilities", "seccomp", "resources", "network"),
        "policy",
    )
    for name, modes in [
        ("filesystem", ("read", "write", "deny")),
        ("devices", ("read", "write", "readwrite")),
    ]:
        rules = value.get(name, [])
        if not isinstance(rules, list):
            raise TypeError(f"{name} must be a list")
        seen = set()
        for rule in rules:
            fields(
                rule,
                ("path", "access", "type")
                if name == "filesystem"
                else ("path", "access"),
                name,
            )
            if (
                not isinstance(rule.get("path"), (str, os.PathLike))
                or not str(rule["path"])
                or "\0" in str(rule["path"])
            ):
                raise ValueError(f"{name}.path must be a non-empty path")
            raw = Path(rule["path"]).expanduser()
            # Resolve once at creation. The backend pins mount sources with fds.
            path = (raw if raw.is_absolute() else cwd / raw).resolve()
            if rule.get("access") not in modes:
                raise ValueError(f"Invalid {name} access: {rule.get('access')}")
            if "type" in rule and rule["type"] not in ("file", "directory"):
                raise ValueError("filesystem.type must be file or directory")
            if str(path) in seen:
                raise ValueError(f"Duplicate {name} path: {path}")
            seen.add(str(path))
            rule["path"] = str(path)
        value[name] = rules
    caps = value.get("capabilities", [])
    if not isinstance(caps, list) or any(
        not isinstance(c, str) or not re.fullmatch(r"(CAP_)?[A-Z][A-Z0-9_]*", c)
        for c in caps
    ):
        raise ValueError("capabilities must contain Linux capability names")
    value["capabilities"] = list(dict.fromkeys(c.removeprefix("CAP_") for c in caps))
    if set(value["capabilities"]) - CAPABILITIES:
        raise ValueError(
            f"Unknown capabilities: {sorted(set(value['capabilities']) - CAPABILITIES)}"
        )
    resources = value.setdefault("resources", {})
    fields(resources, ("memory_bytes", "cpu_count"), "resources")
    for key, limit in resources.items():
        if type(limit) is not int or limit < 1:
            raise ValueError(f"{key} must be a positive integer")
    if "seccomp" in value:
        spec = value["seccomp"]
        fields(spec, ("default", "allow", "deny"), "seccomp")
        if spec.get("default") not in ("allow", "deny"):
            raise ValueError("seccomp.default must be allow or deny")
        key = "deny" if spec["default"] == "allow" else "allow"
        if set(spec) - {"default", key}:
            raise ValueError(f"seccomp accepts {key} with this default")
        names = spec.setdefault(key, [])
        if not isinstance(names, list) or any(
            not isinstance(n, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", n)
            for n in names
        ):
            raise TypeError("seccomp rules must be syscall names")
    network = value.setdefault("network", {"outbound": []})
    if network != "all":
        fields(network, ("outbound",), "network")
        if not isinstance(network.setdefault("outbound", []), list):
            raise TypeError("network.outbound must be a list")
        for rule in network["outbound"]:
            fields(rule, ("interface", "destinations"), "network rule")
            if "interface" in rule and (
                not isinstance(rule["interface"], str)
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", rule["interface"])
            ):
                raise ValueError("Invalid network interface")
            if (
                not isinstance(rule.get("destinations"), list)
                or not rule["destinations"]
            ):
                raise ValueError("destinations must contain CIDR ranges")
            rule["destinations"] = [
                str(ipaddress.ip_network(p, strict=True)) for p in rule["destinations"]
            ]
    return value


def compile_seccomp(spec, output) -> None:
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("Install libseccomp to compile syscall rules")
    lib = ctypes.CDLL(library)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    default_allow = spec["default"] == "allow"
    context = lib.seccomp_init(allow if default_allow else deny)
    if not context:
        raise RuntimeError("seccomp initialization failed")
    try:
        for name in spec.get("deny" if default_allow else "allow", []):
            syscall = lib.seccomp_syscall_resolve_name(name.encode())
            if syscall < 0:
                raise ValueError(f"Unknown syscall: {name}")
            if (
                lib.seccomp_rule_add(
                    context, deny if default_allow else allow, syscall, 0
                )
                < 0
            ):
                raise RuntimeError(f"seccomp rule failed: {name}")
        if lib.seccomp_export_bpf(context, output.fileno()) < 0:
            raise RuntimeError("seccomp export failed")
        output.seek(0)
    finally:
        lib.seccomp_release(context)
