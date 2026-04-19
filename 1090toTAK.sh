#!/usr/bin/env bash
# 1090toTAK launcher — runs 1090toTAK.py inside the venv created by install.1090toTAK.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: venv not found at $SCRIPT_DIR/.venv"
    echo "Run ./install.1090toTAK.sh first."
    exit 1
fi

exec "$VENV_PY" "$SCRIPT_DIR/1090toTAK.py" "$@"
