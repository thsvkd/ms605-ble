#!/usr/bin/env bash
#
# scripts/test.sh — compatibility entrypoint for make test.
#
# No BLE hardware needed. Run make setup and make web-setup first:
#   ./scripts/test.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed. Run make setup for instructions." >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "==> .venv not found; running setup first"
    make setup
fi

exec make test
