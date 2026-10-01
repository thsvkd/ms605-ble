"""ms605 gui end to end: a real `ms605 gui --sim 3` process, uvicorn, sockets,
two WebSocket clients (docs/GUI_API.md section 11.2). Every value is synthetic."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

FIRST_LINE = re.compile(r"^ms605 gui: (http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+))$")
SIM1 = "53494d3630350001"
SIM2 = "53494d3630350002"
REPO = Path(__file__).resolve().parent.parent


class Reader:
    """One WS client: every message it read, in order."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.log: list[dict] = []

    def until(self, predicate, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            message = json.loads(self.ws.recv(timeout=max(0.01, deadline - time.monotonic())))
            self.log.append(message)
            if predicate(message):
                return message


def _sensor(device_id: str, check):
    return lambda m: m["type"] == "sensor" and m["data"]["device_id"] == device_id and check(m["data"])


def _link(device_id: str, link: str):
    return _sensor(device_id, lambda d: d["live"] is not None and d["live"]["link"] == link)


def _launch(tmp_path) -> tuple[subprocess.Popen, str, str]:
    """Start `ms605 gui --sim 3` on a free port; return the process, its port and token."""
    env = {**os.environ, "MS605_DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ms605.cli.cli", "gui", "--sim", "3", "--speed", "20", "--port", "0"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line.rstrip("\n")) for line in proc.stdout], daemon=True).start()
    match = FIRST_LINE.match(lines.get(timeout=10))
    assert match, "first line must be the URL"
    return proc, match.group(2), match.group(3)


def _wait_healthy(http: httpx.Client) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            if http.get("/api/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, "server never answered /api/health"
        time.sleep(0.05)


@pytest.mark.timeout(60)
def test_gui_end_to_end(tmp_path):
    proc, port, token = _launch(tmp_path)
    try:
        base = f"http://127.0.0.1:{port}"
        auth = {"Authorization": f"Bearer {token}"}

        with httpx.Client(base_url=base, headers=auth, timeout=10) as http:
            _wait_healthy(http)

            index = http.get("/")
            assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
            assert index.headers["cache-control"] == "no-store" and '<div id="root">' in index.text
            asset_path = re.search(r'(?:src|href)="(/assets/[^"]+\.js)"', index.text).group(1)
            asset = http.get(asset_path)
            assert asset.status_code == 200 and "javascript" in asset.headers["content-type"]
            assert "immutable" in asset.headers["cache-control"] and len(asset.content) > 1000
            assert http.get("/sensors/anything").text == index.text  # SPA fallback
            assert http.get("/assets/missing.js").status_code == 404

            url = f"ws://127.0.0.1:{port}/ws"
            with connect(url, additional_headers=auth) as ws_a, connect(url, additional_headers=auth) as ws_b:
                a, b = Reader(ws_a), Reader(ws_b)
                for reader in (a, b):
                    snapshot = reader.until(lambda m: True)
                    assert snapshot["type"] == "snapshot"
                    assert snapshot["data"]["server"]["sim"] == {"count": 3, "speed": 20.0}
                    assert snapshot["data"]["sensors"] == []

                assert http.post("/api/gather/start").status_code == 200
                assert http.post("/api/sim/press/1").status_code == 204
                assert http.post("/api/sim/press/2").status_code == 204
                for device_id in (SIM1, SIM2):
                    sensor = a.until(_link(device_id, "connected"))
                    assert sensor["data"]["registry"] is None

                site = http.post("/api/sites", json={"name": "Lab A"})
                assert site.status_code == 201 and site.json()["site_id"] == "lab-a"
                body = {"device_id": SIM1, "site_id": "lab-a", "alias": "Sensor 1", "location": "north wall"}
                assert http.post("/api/sensors", json=body).status_code == 201
                named = a.until(_sensor(SIM1, lambda d: d["registry"] is not None))
                assert named["data"]["registry"]["alias"] == "Sensor 1"

                registry = json.loads((tmp_path / "cal_results" / "sim" / "registry.json").read_text("utf-8"))
                assert registry["sites"]["lab-a"]["name"] == "Lab A"
                assert registry["sensors"][SIM1]["alias"] == "Sensor 1"
                assert "02:00:00:00:00:01" in registry["sensors"][SIM1]["addresses"].values()

                assert http.post("/api/sim/drop/2").status_code == 204
                a.until(_link(SIM2, "lost"))

                assert http.post("/api/gather/stop").status_code == 200
                assert http.post("/api/release", json={}).status_code == 204
                a.until(_sensor(SIM1, lambda d: d["live"] is None))
                a.until(lambda m: m["type"] == "sensor_removed" and m["data"]["device_id"] == SIM2)

                last = a.log[-1]["seq"]
                b.until(lambda m: m["seq"] >= last)
                assert [(m["seq"], m["type"]) for m in a.log] == [(m["seq"], m["type"]) for m in b.log]
                seqs = [m["seq"] for m in a.log]
                assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) in (0, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.timeout(30)
def test_sigint_with_open_websockets_exits_promptly(tmp_path):
    proc, port, token = _launch(tmp_path)
    try:
        auth = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=auth, timeout=10) as http:
            _wait_healthy(http)
            with connect(f"ws://127.0.0.1:{port}/ws", additional_headers=auth) as ws:
                reader = Reader(ws)
                reader.until(lambda m: m["type"] == "snapshot")
                assert http.post("/api/gather/start").status_code == 200
                assert http.post("/api/sim/press-all").status_code == 204
                for device_id in (SIM1, SIM2):
                    reader.until(_link(device_id, "connected"))
                started = time.monotonic()
                proc.send_signal(signal.SIGINT)  # the WS is still open
                with pytest.raises(ConnectionClosed) as closed:
                    reader.until(lambda m: False)
                # uvicorn closes open sockets with 1012 before the lifespan's close(1001) runs
                assert closed.value.rcvd is not None and closed.value.rcvd.code in (1001, 1012)
        assert proc.wait(timeout=10) in (0, 130)
        assert time.monotonic() - started < 5  # not the 5 s graceful-shutdown timeout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
