"""Run the checkout entrypoints without installing dependencies or using BLE."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    shutil.copytree(ROOT / "scripts", root / "scripts")
    if (ROOT / "Makefile").exists():
        shutil.copy(ROOT / "Makefile", root)
    (root / ".venv").mkdir()
    (root / "web").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uv", "npm"):
        tool = bin_dir / name
        tool.write_text(
            '#!/bin/bash\n'
            'printf "%s" "${0##*/}" >> "$COMMAND_LOG"\n'
            'printf " <%s>" "$@" >> "$COMMAND_LOG"\n'
            'printf "\\n" >> "$COMMAND_LOG"\n'
            'if [ "$*" = "$FAIL_COMMAND" ]; then exit 23; fi\n'
            'if [ "$1" = "--version" ]; then echo "uv 0.0.0"; fi\n'
        )
        tool.chmod(0o755)
    log = tmp_path / "commands.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log), "FAIL_COMMAND": ""}
    # Keep a parent make invocation from altering these subprocesses.
    for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
        env.pop(key, None)

    def run(*args, fail=""):
        result = subprocess.run(
            args, cwd=tmp_path, env={**env, "FAIL_COMMAND": fail}, capture_output=True, text=True, timeout=10
        )
        return result, log.read_text().splitlines() if log.exists() else []

    return root, run


def test_run_script_preserves_arguments_from_another_directory(checkout):
    root, run = checkout
    result, calls = run(str(root / "scripts/run.sh"), "--address", "sensor with spaces", "gui", "--port", "0")
    assert result.returncode == 0, result.stderr
    assert calls == ["uv <run> <ms605> <--address> <sensor with spaces> <gui> <--port> <0>"]


def test_setup_script_runs_without_node(checkout):
    root, run = checkout
    result, calls = run(str(root / "scripts/setup.sh"))
    assert result.returncode == 0, result.stderr
    assert calls == ["uv <--version>", "uv <sync>"]


def test_legacy_test_script_stops_on_python_failure(checkout):
    root, run = checkout
    result, calls = run(str(root / "scripts/test.sh"), fail="run pytest -q")
    assert result.returncode != 0
    assert calls == ["uv <run> <pytest> <-q>"]


@pytest.mark.parametrize(
    ("target", "args", "expected"),
    [
        ("cli", '--address "sensor with spaces" read', "uv <run> <ms605> <--address> <sensor with spaces> <read>"),
        ("run", "--help", "uv <run> <ms605> <--help>"),
        ("gui", "--sim 7 --speed 20", "uv <run> <ms605> <gui> <--lan> <--sim> <7> <--speed> <20>"),
    ],
)
def test_make_launches_cli_and_gui_without_node(checkout, target, args, expected):
    root, run = checkout
    result, calls = run("make", "-C", str(root), target, f"ARGS={args}")
    assert result.returncode == 0, result.stderr
    assert calls == [expected]


def test_make_checks_python_and_frontend(checkout):
    root, run = checkout
    result, calls = run("make", "-C", str(root), "test")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "uv <run> <pytest> <-q>",
        "npm <--prefix> <web> <test>",
        "uv <run> <ruff> <check> <.>",
        "npm <--prefix> <web> <run> <typecheck>",
    ]


def test_make_reports_frontend_failure(checkout):
    root, run = checkout
    result, calls = run("make", "-C", str(root), "test", fail="--prefix web test")
    assert result.returncode != 0
    assert calls == ["uv <run> <pytest> <-q>", "npm <--prefix> <web> <test>"]


def test_legacy_test_script_also_checks_frontend(checkout):
    root, run = checkout
    result, calls = run(str(root / "scripts/test.sh"))
    assert result.returncode == 0, result.stderr
    assert "npm <--prefix> <web> <test>" in calls
    assert "npm <--prefix> <web> <run> <typecheck>" in calls
