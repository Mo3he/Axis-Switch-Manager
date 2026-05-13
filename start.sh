#!/usr/bin/env bash
# Start the Axis Switch Manager

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv/bin/python"

echo "Starting Axis Switch Manager..."
echo "Open http://localhost:8000 in your browser"
echo "Press Ctrl+C to stop"
echo ""

cd "$SCRIPT_DIR"
"$VENV" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
