#!/usr/bin/env bash
# Enkiball Teacher — stop script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.teacher.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "Teacher is not running (no PID file)."
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping Teacher (PID $PID) ..."
    kill "$PID"
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
        echo "Force killing ..."
        kill -9 "$PID" 2>/dev/null || true
    fi
    echo "Teacher stopped."
else
    echo "Teacher was not running (stale PID file)."
fi

rm -f "$PID_FILE"
