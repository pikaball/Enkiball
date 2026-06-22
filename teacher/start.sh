#!/usr/bin/env bash
# Enkiball Teacher — start script
# Usage:  ./start.sh [host] [port]
#         ./start.sh 0.0.0.0 8080

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PID_FILE="$SCRIPT_DIR/.teacher.pid"
CONFIG_FILE="$SCRIPT_DIR/config/enki.json"
HOST="${1:-127.0.0.1}"
PORT="${2:-8765}"

# Check venv exists
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "ERROR: Virtual environment not found at $VENV_DIR"
    echo "Run 'uv sync' in the project root first."
    exit 1
fi

# Check config exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Teacher config not found at $CONFIG_FILE"
    echo "Create it from the base config: cp ~/.config/enkiball/enki.json $CONFIG_FILE"
    exit 1
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Teacher is already running (PID $OLD_PID) on http://$HOST:$PORT"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# Install teacher dependencies if needed
cd "$PROJECT_DIR"
uv pip install -q fastapi uvicorn 2>/dev/null || true

# Start uvicorn
echo "Starting Enkiball Teacher on http://$HOST:$PORT ..."
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/python" -m uvicorn main:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level warning \
    &
PID=$!
echo "$PID" > "$PID_FILE"

# Wait a moment to check it started
sleep 1
if kill -0 "$PID" 2>/dev/null; then
    echo "Teacher running (PID $PID) on http://$HOST:$PORT"
else
    echo "ERROR: Teacher failed to start. Check logs above."
    rm -f "$PID_FILE"
    exit 1
fi
