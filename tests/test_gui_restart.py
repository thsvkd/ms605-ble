"""The Make launcher replaces a GUI, never an unrelated port owner."""

from __future__ import annotations

import contextlib
import os
import queue
import re
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r"^ms605 gui: http://127\.0\.0\.1:(\d+)/\?t=([\w-]+)$")
pytestmark = pytest.mark.skipif(os.name != "posix" or not shutil.which("lsof"), reason="POSIX lsof launcher")


@pytest.mark.parametrize(
    "command, expected",
    [
        (f"{sys.executable} {ROOT / '.venv/bin/ms605'} gui --lan", True),
        (f"{sys.executable} -m ms605.cli.cli gui --sim 1", True),
        ('python -c "print(1)" ms605 gui', False),
        ("worker --job ms605 gui", False),
        ("/tmp/python-worker /tmp/ms605 gui", False),
        ("python /tmp/other/ms605 gui", False),
        ("/tmp/ms605 gui", False),
    ],
)
def test_process_identity_requires_a_gui_entrypoint(command, expected):
    identify = runpy.run_path(str(ROOT / "scripts/gui.py"))["_is_gui"]
    assert identify(command) is expected


@pytest.fixture
def launch(tmp_path):
    processes = []

    def start(*args, script=False):
        command = [sys.executable, str(ROOT / "scripts/gui.py")] if script else [
            sys.executable, "-m", "ms605.cli.cli", "gui"
        ]
        process = subprocess.Popen(
            [*command, "--sim", "1", *args],
            cwd=ROOT,
            env={**os.environ, "MS605_DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        processes.append(process)
        lines = queue.Queue()

        def read():
            for line in process.stdout:
                lines.put(line.strip())
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        output = []
        while True:
            line = lines.get(timeout=15)
            assert line is not None, "GUI failed to start: " + "\n".join(output)
            output.append(line)
            match = URL.match(line)
            if match:
                port, token = match.groups()
                # A URL is printed before uvicorn enters its serving loop.
                with httpx.Client(trust_env=False, timeout=5) as client:
                    for _ in range(50):
                        try:
                            if client.get(f"http://127.0.0.1:{port}/api/health").status_code == 200:
                                return process, port, token
                        except httpx.TransportError:
                            pass
                        threading.Event().wait(0.05)
                raise AssertionError("GUI did not become healthy")

    yield start
    for process in processes:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def test_launcher_replaces_gui_on_selected_port(launch):
    old, port, old_token = launch("--port", "0")
    new, new_port, new_token = launch("--port", port, script=True)
    old.wait(timeout=10)
    assert new_port == port
    assert new.poll() is None
    assert new_token != old_token
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
        assert client.get("/api/state", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
        assert client.get("/api/state", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_launcher_port_zero_does_not_stop_existing_gui(launch):
    old, old_port, _ = launch("--port", "0")
    _, new_port, _ = launch("--port", "0", script=True)
    assert old.poll() is None
    assert new_port != old_port


def test_launcher_leaves_unrelated_listener_alive(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/gui.py"), "--sim", "1", "--port", str(port)],
            cwd=ROOT,
            env={**os.environ, "MS605_DATA_DIR": str(tmp_path)},
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2
        assert "MS605 GUI" in result.stderr
        # The current pytest process owns this socket and must remain alive.
        with socket.socket() as probe, pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))


@pytest.mark.parametrize("args, code", [(["--help"], 0), (["--port", "invalid"], 2)])
def test_launcher_validates_arguments_without_stopping_server(launch, args, code):
    old, port, _ = launch("--port", "0")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/gui.py"), "--port", port, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == code
    if code == 2:
        assert "argument --port:" in result.stderr
    assert old.poll() is None
