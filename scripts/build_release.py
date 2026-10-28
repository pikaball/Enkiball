"""Build distributions and remove local ownership metadata from sdists."""

from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "build"], cwd=root, check=True)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    for path in (root / "dist").glob("*.tar.gz"):
        output = io.BytesIO()
        with tarfile.open(path, "r:gz") as source:
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as target:
                    for member in source:
                        member.uid = member.gid = 0
                        member.uname = member.gname = ""
                        member.mtime = epoch
                        for key in ("uid", "gid", "uname", "gname", "mtime", "atime", "ctime"):
                            member.pax_headers.pop(key, None)
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(output.getvalue())
        temporary.replace(path)


if __name__ == "__main__":
    main()
