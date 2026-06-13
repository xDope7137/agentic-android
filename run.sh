#!/usr/bin/env bash
# Agentic Android launcher — runs the agent in its own virtualenv, from anywhere.
#   ./run.sh --list-devices
#   ./run.sh                       # uses agentic-android.toml
#   ./run.sh --provider openai "Install WhatsApp"
# First run creates the virtualenv and installs everything.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "[Agentic Android] First run: creating virtualenv and installing dependencies..." >&2
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[Agentic Android] ERROR: python3 not found. Install Python 3.10+ and retry." >&2
    exit 1
  fi
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install --upgrade pip >/dev/null
  "$DIR/.venv/bin/pip" install -e "$DIR"
fi

exec "$PY" -m agentic_android "$@"
