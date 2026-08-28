#!/usr/bin/env bash
#
# scripts/run.sh — launch the MS605 interactive BLE app.
#
# Any arguments are passed straight through to the `ms605` console script, e.g.:
#   ./scripts/run.sh                       # interactive app (connect + calibrate + adjust)
#   ./scripts/run.sh calibrate             # run auto-calibration directly, then exit
#   ./scripts/run.sh read                  # print current config, then exit
#   ./scripts/run.sh set-zone 95,40 85,40 75,40 60,40 55,40 40,35 35,28
#   ./scripts/run.sh set-sensitivity 3     # 1=LOW 2=MEDIUM 3=HIGH 4=CUSTOM
# Note: global options go BEFORE the command. To target one specific device
# (handy when several are advertising), pass its address/UUID or name:
#   ./scripts/run.sh --address <DEVICE-UUID> calibrate
#   ./scripts/run.sh --scan-secs 8 calibrate
# Connect several sensors at once and batch-calibrate them (immediate or
# scheduled) with the `calibrate` subcommand:
#   ./scripts/run.sh calibrate --collect          # gather as buttons are pressed
#   ./scripts/run.sh calibrate --schedule 02:00   # fire unattended at 02:00
# See all commands/options:
#   ./scripts/run.sh --help
#
# For the low-level driver CLI, call `uv run ms605-driver ...` directly.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed. Run ./scripts/setup.sh for instructions." >&2
    exit 1
fi

# Auto-bootstrap the environment on first run.
if [ ! -d ".venv" ]; then
    echo "==> .venv not found; running setup first"
    ./scripts/setup.sh
fi

# uv keeps .venv in sync with pyproject.toml, then runs the app.
exec uv run ms605 "$@"
