#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Run as root.' >&2; exit 1; }
systemctl disable --now enkisandbox.service
rm -f /etc/systemd/system/enkisandbox.service /usr/local/bin/enkisandbox-client
systemctl daemon-reload
rm -rf /opt/enkisandbox
printf '%s\n' 'Runtime removed. Authorization configuration and enkisandbox group retained.'
