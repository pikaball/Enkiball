#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer as root.' >&2
    exit 1
fi
sandbox_source=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
for sandbox_dependency in python3 cc make bwrap systemctl setfacl; do
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
if [ ! -e /etc/enkisandbox/socket-users ]; then
    install -m 600 /dev/null /etc/enkisandbox/socket-users
fi
if [ -n "${SUDO_UID:-}" ] && [ "$SUDO_UID" != 0 ]; then
    case "$SUDO_UID" in *[!0-9]*) echo 'Invalid SUDO_UID' >&2; exit 1;; esac
    if ! grep -Fxq "$SUDO_UID" /etc/enkisandbox/socket-users; then
        printf '%s\n' "$SUDO_UID" >> /etc/enkisandbox/socket-users
    fi
fi
sed "s/ENKISANDBOX_GID/$sandbox_gid/" "$sandbox_source/systemd/enkisandbox.service" > /etc/systemd/system/enkisandbox.service
systemctl daemon-reload
systemctl enable enkisandbox.service
systemctl restart enkisandbox.service
cat <<'EOF'
EnkiSandbox runtime installed. When run with sudo, the invoking user's UID is
granted socket access immediately, including in existing sessions.
To authorize other users, add their numeric UIDs (one per line) to
/etc/enkisandbox/socket-users, then restart the service.
Optional: Configure per-UID policy limits in /etc/enkisandbox/authorization.json
(see sandbox/README.md). Unconfigured limits do not restrict requests.
After changing authorization, restart the service:
       sudo systemctl restart enkisandbox.service
EOF
