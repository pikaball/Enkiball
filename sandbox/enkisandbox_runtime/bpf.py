"""Cgroup eBPF network and device policies."""

from __future__ import annotations

import ctypes
import ctypes.util
import ipaddress
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path


def policy_source(network, devices):
    source = [
        "#define SEC(x) __attribute__((section(x), used))",
        'char LICENSE[] SEC("license") = "GPL";',
        "struct skb { unsigned len, pkt_type, mark, queue_mapping, protocol, vlan_present, vlan_tci, vlan_proto, priority, ingress_ifindex, ifindex; };",
        "static long (*load)(void *, unsigned, void *, unsigned) = (void *)26;",
    ]
    if network:
        source += [
            'SEC("cgroup_skb/egress") int outbound(struct skb *s) {',
            "unsigned char version = 0, address[16] = {};",
            "if (load(s, 0, &version, 1)) return 0;",
            "version >>= 4;",
            "if (version == 4) { if (load(s, 16, address, 4)) return 0; }",
            "else if (version == 6) { if (load(s, 24, address, 16)) return 0; } else return 0;",
        ]
        for rule in network["outbound"]:
            interface = (
                socket.if_nametoindex(rule["interface"])
                if "interface" in rule
                else None
            )
            for text in rule["destinations"]:
                net = ipaddress.ip_network(text)
                terms = [f"version == {net.version}"]
                if interface is not None:
                    terms.append(f"s->ifindex == {interface}")
                for i, (byte, mask) in enumerate(
                    zip(net.network_address.packed, net.netmask.packed)
                ):
                    if mask:
                        terms.append(f"(address[{i}] & {mask}) == {byte}")
                source.append("if (" + " && ".join(terms) + ") return 1;")
        source += ["return 0; }"]
    if devices:
        source += [
            "struct device { unsigned access_type, major, minor; };",
            'SEC("cgroup/dev") int device_access(struct device *d) {',
        ]
        seen = {}
        for rule in devices:
            info = Path(rule["path"]).stat()
            if not (stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)):
                raise ValueError(f"Device node required: {rule['path']}")
            key = (
                os.major(info.st_rdev),
                os.minor(info.st_rdev),
                2 if stat.S_ISCHR(info.st_mode) else 1,
            )
            allowed = {"read": 2, "write": 4, "readwrite": 6}[rule["access"]]
            if key in seen and seen[key] != allowed:
                raise ValueError("Device aliases require matching access")
            seen[key] = allowed
        for (major, minor, kind), allowed in seen.items():
            source.append(
                f"if (d->major == {major} && d->minor == {minor} && (d->access_type & 65535) == {kind}) return (((d->access_type >> 16) & ~{allowed}) == 0);"
            )
        source += [
            "if ((d->access_type & 65535) == 2 && (((d->major == 1 && (d->minor == 3 || d->minor == 5 || d->minor == 7 || d->minor == 8 || d->minor == 9)) || (d->major == 5 && (d->minor == 0 || d->minor == 2)) || (d->major >= 136 && d->major <= 143)))) return (((d->access_type >> 16) & ~6) == 0);",
            "return 0; }",
        ]
    return "\n".join(source)


class CgroupPolicy:
    def __init__(self, path, network, devices):
        self.object = None
        library, clang = ctypes.util.find_library("bpf"), shutil.which("clang")
        if not library or not clang:
            raise RuntimeError(
                "Network and directional device rules require libbpf and clang with BPF support"
            )
        self.lib = lib = ctypes.CDLL(library)
        lib.libbpf_get_error.argtypes = [ctypes.c_void_p]
        lib.libbpf_get_error.restype = ctypes.c_long
        lib.bpf_object__open_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        lib.bpf_object__open_file.restype = ctypes.c_void_p
        lib.bpf_object__load.argtypes = [ctypes.c_void_p]
        lib.bpf_object__find_program_by_name.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        lib.bpf_object__find_program_by_name.restype = ctypes.c_void_p
        lib.bpf_program__fd.argtypes = [ctypes.c_void_p]
        lib.bpf_program__fd.restype = ctypes.c_int
        lib.bpf_prog_attach.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        lib.bpf_object__close.argtypes = [ctypes.c_void_p]
        try:
            with tempfile.TemporaryDirectory(prefix="enkiball-bpf-") as directory:
                src, obj = Path(directory) / "policy.c", Path(directory) / "policy.o"
                src.write_text(policy_source(network, devices))
                result = subprocess.run(
                    [
                        clang,
                        "-O2",
                        "-g",
                        "-target",
                        "bpf",
                        "-c",
                        str(src),
                        "-o",
                        str(obj),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode:
                    raise RuntimeError(
                        f"BPF compilation failed: {result.stderr.strip()}"
                    )
                handle = lib.bpf_object__open_file(os.fsencode(obj), None)
                if not handle or lib.libbpf_get_error(handle):
                    raise RuntimeError("BPF object initialization failed")
                self.object = handle
                if lib.bpf_object__load(handle):
                    raise RuntimeError(
                        "Cgroup policy requires host BPF privileges and kernel BPF support"
                    )
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for name, enabled in [
                    (b"outbound", network),
                    (b"device_access", devices),
                ]:
                    if enabled:
                        program = lib.bpf_object__find_program_by_name(handle, name)
                        if not program:
                            raise RuntimeError("BPF program lookup failed")
                        attach_type = 1 if name == b"outbound" else 6
                        if lib.bpf_prog_attach(
                            lib.bpf_program__fd(program), fd, attach_type, 2
                        ):
                            raise RuntimeError("Cgroup policy attachment failed")
            finally:
                os.close(fd)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.object:
            self.lib.bpf_object__close(self.object)
            self.object = None
