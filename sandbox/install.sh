#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer as root.' >&2
    exit 1
fi
sandbox_source=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
for sandbox_dependency in python3 cc make bwrap systemctl; do
    command -v "$sandbox_dependency" >/dev/null || { echo "Missing dependency: $sandbox_dependency" >&2; exit 1; }
done
[ -f /sys/fs/cgroup/cgroup.controllers ] || { echo 'cgroup v2 is required' >&2; exit 1; }
if ! getent group enkisandbox >/dev/null; then
    groupadd --system enkisandbox
fi
sandbox_gid=$(getent group enkisandbox | cut -d: -f3)
make -C "$sandbox_source" install-client
python3 -m venv /opt/enkisandbox
/opt/enkisandbox/bin/pip install --upgrade "$sandbox_source"
install -d -m755 /etc/enkisandbox
if [ ! -e /etc/enkisandbox/authorization.json ]; then
    printf '{}\n' > /etc/enkisandbox/authorization.json
    chmod 600 /etc/enkisandbox/authorization.json
fi
sed "s/ENKISANDBOX_GID/$sandbox_gid/" "$sandbox_source/systemd/enkisandbox.service" > /etc/systemd/system/enkisandbox.service
systemctl daemon-reload
systemctl enable enkisandbox.service
systemctl restart enkisandbox.service
