#!/usr/bin/env bash
#
# scripts/setup.sh — one-shot environment setup for the MS605 BLE package.
#
# Uses uv to create a reproducible virtualenv (Python 3.12, pinned in
# .python-version) and install bleak with the macOS CoreBluetooth backend,
# plus the dev dependencies (pytest, ruff) used by scripts/test.sh.
#
# Only sets up the environment — run scripts/test.sh afterward to verify it,
# or scripts/run.sh to launch the app.
#
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> MS605 BLE package — setup"

# 1. Ensure uv is available.
if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed." >&2
    echo "       Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "       Then re-run:      ./scripts/setup.sh" >&2
    exit 1
fi
echo "==> uv $(uv --version | awk '{print $2}') found"

# 2. Remove a stale/foreign virtualenv (the repo shipped a Linux venv that
#    cannot run on macOS). Safe to delete — uv rebuilds .venv from pyproject.
if [ -d "venv" ]; then
    echo "==> removing stale prebuilt 'venv/' (not usable on this machine)"
    rm -rf venv
fi

# 3. Create .venv and install dependencies (incl. dev group) from pyproject.toml.
echo "==> uv sync (creating .venv + installing bleak/pytest/ruff)"
uv sync

echo
echo "Setup complete."
echo "Verify the checkout:      ./scripts/test.sh"
echo "Run the interactive app:  ./scripts/run.sh"
