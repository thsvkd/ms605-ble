#!/usr/bin/env bash
#
# scripts/test.sh — verify the checkout: offline pytest suite + ruff lint.
#
# No BLE hardware needed (the whole suite is offline). Run after
# scripts/setup.sh, or any time you want to check your working tree:
#   ./scripts/test.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed. Run ./scripts/setup.sh for instructions." >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "==> .venv not found; running setup first"
    ./scripts/setup.sh
fi

echo "==> pytest"
uv run pytest -q

echo
echo "==> ruff check"
uv run ruff check .

echo
echo "All checks passed."
