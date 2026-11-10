"""Instance budgets and execution subgroups on delegated cgroup v2."""

from __future__ import annotations

import os
import time
from pathlib import Path


def cpu_set(text):
    result = set()
    for item in text.strip().split(","):
        if item:
            bounds = [int(n) for n in item.split("-")]
            result.update(range(bounds[0], bounds[-1] + 1))
    return result


def remove_group(path: Path):
    if not path.exists():
        return
    (path / "cgroup.kill").write_text("1")
    for child in path.iterdir():
        if child.is_dir():
            remove_group(child)
    for _ in range(200):
        try:
            path.rmdir()
            return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError(f"cgroup cleanup failed: {path}")


class ResourceGroup:
    def __init__(self, root: Path, identity: str, limits):
        needed = {"memory" if k == "memory_bytes" else "cpuset" for k in limits}
        active = set((root / "cgroup.subtree_control").read_text().split())
        if not needed <= active:
            raise RuntimeError("Delegate memory and cpuset controllers to the backend")
        self.path = root / ("sandbox-" + identity)
        self.cpus = []
        self.path.mkdir(mode=0o700)
        try:
            if "memory_bytes" in limits:
                (self.path / "memory.max").write_text(str(limits["memory_bytes"]))
                (self.path / "memory.swap.max").write_text("0")
                (self.path / "memory.oom.group").write_text("1")
            if "cpu_count" in limits:
                available = cpu_set(
                    (root / "cpuset.cpus.effective").read_text()
                ) & os.sched_getaffinity(0)
                if limits["cpu_count"] > len(available):
                    raise ValueError(
                        f"cpu_count exceeds {len(available)} available processors"
                    )
                self.cpus = sorted(available)[: limits["cpu_count"]]
                (self.path / "cpuset.mems").write_text(
                    (root / "cpuset.mems.effective").read_text()
                )
                (self.path / "cpuset.cpus").write_text(",".join(map(str, self.cpus)))
            if needed:
                (self.path / "cgroup.subtree_control").write_text(
                    " ".join("+" + n for n in needed)
                )
            if not (self.path / "cgroup.kill").exists():
                raise RuntimeError("Linux with cgroup.kill support is required")
        except BaseException:
            remove_group(self.path)
            raise

    def task(self, execution_id):
        path = self.path / ("task-" + execution_id)
        path.mkdir(mode=0o700)
        return path

    def inspect(self):
        result = {"cpus": self.cpus, "cgroup": str(self.path)}
        for name in ("memory.current", "memory.peak", "memory.events"):
            p = self.path / name
            if p.exists():
                result[name] = p.read_text().strip()
        return result

    def close(self):
        remove_group(self.path)
