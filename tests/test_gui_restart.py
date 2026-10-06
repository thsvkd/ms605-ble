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
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from ms605.gui import cli as gui_cli

ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r"^ms605 gui: https://127\.0\.0\.1:(\d+)/\?t=([\w-]+)$")
pytestmark = pytest.mark.skipif(os.name != "posix" or not shutil.which("lsof"), reason="POSIX lsof launcher")


def test_gui_tokens_are_secure_persistent_and_separate(tmp_path, monkeypatch):
    generated = iter(("A" * 43, "B" * 43))
    monkeypatch.setattr(gui_cli.secrets, "token_urlsafe", lambda _size: next(generated))
    real_path = tmp_path / "cal_results" / gui_cli.GUI_TOKEN_FILE
    sim_path = tmp_path / "cal_results" / "sim" / gui_cli.GUI_TOKEN_FILE

    assert gui_cli._load_or_create_gui_token(real_path) == "A" * 43
    assert gui_cli._load_or_create_gui_token(real_path) == "A" * 43
    assert gui_cli._load_or_create_gui_token(sim_path) == "B" * 43
    assert real_path.stat().st_mode & 0o777 == 0o600
    assert sim_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("content", "mode"), [("damaged\n", 0o600), ("A" * 43 + "\n", 0o644)])
def test_invalid_gui_token_file_fails_closed(tmp_path, content, mode):
    path = tmp_path / gui_cli.GUI_TOKEN_FILE
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)

    with pytest.raises(gui_cli.GuiTokenError):
        gui_cli._load_or_create_gui_token(path)


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
                ca = tmp_path / "cal_results" / "gui_tls" / "ca.pem"
                tls = ssl.create_default_context(cafile=ca)
                with httpx.Client(trust_env=False, timeout=5, verify=tls) as client:
                    for _ in range(50):
                        try:
                            if client.get(f"https://127.0.0.1:{port}/api/health").status_code == 200:
                                return process, port, token, ca
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
    old, port, old_token, ca = launch("--port", "0")
    new, new_port, new_token, _ = launch("--port", port, script=True)
    old.wait(timeout=10)
    assert new_port == port
    assert new.poll() is None
    assert new_token == old_token
    with httpx.Client(
        base_url=f"https://127.0.0.1:{port}", trust_env=False, verify=ssl.create_default_context(cafile=ca)
    ) as client:
        assert client.get("/api/state", headers={"Authorization": f"Bearer {old_token}"}).status_code == 200
        assert client.get("/api/state", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_launcher_replaces_server_ble_gui_on_selected_port(launch):
    old, port, token, _ = launch("--port", "0", "--ble-transport", "server")
    new, new_port, new_token, _ = launch("--port", port, "--ble-transport", "server", script=True)
    old.wait(timeout=10)
    assert new_port == port
    assert new.poll() is None
    assert new_token == token


def test_server_ble_lock_rejects_a_second_port_zero_process_then_releases(launch, tmp_path):
    first, _, token, _ = launch("--port", "0", "--ble-transport", "server")
    lock_file = tmp_path / "cal_results" / "sim" / gui_cli.GUI_SERVER_LOCK_FILE
    assert lock_file.stat().st_mode & 0o777 == 0o600
    second = subprocess.run(
        [
            sys.executable, "-m", "ms605.cli.cli", "gui", "--sim", "1", "--port", "0",
            "--ble-transport", "server",
        ],
        cwd=ROOT,
        env={**os.environ, "MS605_DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert second.returncode == 2
    assert "이미 같은 데이터 디렉터리를 사용하는 서버 Bluetooth GUI" in second.stderr
    assert first.poll() is None

    os.killpg(first.pid, signal.SIGINT)
    first.wait(timeout=10)
    restarted, _, restarted_token, _ = launch("--port", "0", "--ble-transport", "server")
    assert restarted.poll() is None
    assert restarted_token == token


def test_deleting_saved_token_rotates_it_on_restart(launch, tmp_path):
    old, port, old_token, ca = launch("--port", "0")
    token_file = tmp_path / "cal_results" / "sim" / "gui_access_token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    token_file.unlink()

    new, new_port, new_token, _ = launch("--port", port, script=True)
    old.wait(timeout=10)
    assert new_port == port
    assert new.poll() is None
    assert new_token != old_token
    with httpx.Client(
        base_url=f"https://127.0.0.1:{port}", trust_env=False, verify=ssl.create_default_context(cafile=ca)
    ) as client:
        assert client.get("/api/state", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
        assert client.get("/api/state", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_launcher_port_zero_does_not_stop_existing_gui(launch):
    old, old_port, _, _ = launch("--port", "0")
    _, new_port, _, _ = launch("--port", "0", script=True)
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
        assert not (tmp_path / "cal_results" / "sim" / "gui_access_token").exists()
        # The current pytest process owns this socket and must remain alive.
        with socket.socket() as probe, pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))


@pytest.mark.parametrize("args, code", [(["--help"], 0), (["--port", "invalid"], 2)])
def test_launcher_validates_arguments_without_stopping_server(launch, args, code):
    old, port, _, _ = launch("--port", "0")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/gui.py"), "--port", port, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == code
    if code == 2:
        assert "argument --port:" in result.stderr
    assert old.poll() is None
