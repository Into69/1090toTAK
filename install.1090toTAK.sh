#!/usr/bin/env bash
# 1090toTAK installer
# Clones the repo, creates a Python venv, installs requirements,
# and offers to install readsb (recommended ADS-B decoder).
set -e

REPO_URL="https://github.com/Into69/1090toTAK.git"
REPO_DIR="1090toTAK"
PY_BIN="${PYTHON:-python3}"

echo "==> 1090toTAK installer"

# --- Prerequisites --------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' is required but not installed."; exit 1; }; }
need git
need "$PY_BIN"

# --- Clone or update ------------------------------------------------------
if [ -d "$REPO_DIR/.git" ]; then
    echo "==> Repo already exists; pulling latest"
    git -C "$REPO_DIR" pull --ff-only
else
    echo "==> Cloning $REPO_URL"
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# --- Python venv ----------------------------------------------------------
if [ ! -d ".venv" ]; then
    echo "==> Creating Python venv (.venv)"
    "$PY_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> Upgrading pip"
pip install --quiet --upgrade pip

echo "==> Installing requirements"
pip install -r requirements.txt

# --- readsb check ---------------------------------------------------------
echo
if command -v readsb >/dev/null 2>&1; then
    echo "==> readsb already installed"
else
    echo "==> readsb (recommended C-based ADS-B decoder) not found."
    if [ -t 0 ]; then
        read -r -p "    Install readsb now via the official wiedehopf installer? [y/N] " ans
    else
        ans="n"
    fi
    case "$ans" in
        [yY]|[yY][eE][sS])
            if ! command -v wget >/dev/null 2>&1; then
                echo "    ERROR: wget is required to fetch the readsb installer."
            else
                echo "    Running readsb installer (sudo required)..."
                sudo bash -c "$(wget -nv -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"
            fi
            ;;
        *)
            echo "    Skipping readsb install. You can install it later from:"
            echo "      https://github.com/wiedehopf/readsb"
            ;;
    esac
fi

cat <<EOF

==> Install complete.

To run 1090toTAK:
  cd $REPO_DIR
  ./1090toTAK.sh

(or activate the venv manually: source .venv/bin/activate && python 1090toTAK.py)

Web UI: http://localhost:8080
EOF
