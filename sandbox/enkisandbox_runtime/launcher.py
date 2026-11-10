"""Single-threaded child: join task budget before dropping the caller identity."""

import ctypes
import json
import os
import sys
from pathlib import Path


def identity(uid, gid, groups):
    if os.geteuid() == 0:
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
    elif (os.getuid(), os.getgid()) != (uid, gid):
        raise PermissionError("backend cannot assume the caller identity")


if __name__ == "__main__":
    with os.fdopen(int(sys.argv[1])) as source:
        spec = json.load(source)
    if spec.get("cgroup"):
        (Path(spec["cgroup"]) / "cgroup.procs").write_text(str(os.getpid()))
    identity(spec["uid"], spec["gid"], spec["groups"])
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    os.execve(spec["argv"][0], spec["argv"], spec["env"])
