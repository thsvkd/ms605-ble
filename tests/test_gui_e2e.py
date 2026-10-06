"""ms605 gui end to end: a real `ms605 gui --sim 3` process, uvicorn, sockets,
two WebSocket clients (docs/GUI_API.md 11.2, and 14.9.3 for the M3 calibration
run). Every value is synthetic."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect as async_connect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

FIRST_LINE = re.compile(r"^ms605 gui: (https://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+))$")
SIM1 = "53494d3630350001"
SIM2 = "53494d3630350002"
SIM3 = "53494d3630350003"
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

    def drain(self, seconds: float) -> list[dict]:
        """Read whatever arrives for `seconds` (logged too) and return it."""
        deadline = time.monotonic() + seconds
        got: list[dict] = []
        while (left := deadline - time.monotonic()) > 0:
            try:
                got.append(json.loads(self.ws.recv(timeout=left)))
            except TimeoutError:
                break
            self.log.append(got[-1])
        return got


def _sensor(device_id: str, check):
    return lambda m: m["type"] == "sensor" and m["data"]["device_id"] == device_id and check(m["data"])


def _link(device_id: str, link: str):
    return _sensor(device_id, lambda d: d["live"] is not None and d["live"]["link"] == link)


def _launch(tmp_path, speed: int = 20) -> tuple[subprocess.Popen, str, str]:
    """Start `ms605 gui --sim 3 --speed <speed>` on a free port; return the process, its port and token."""
    env = {**os.environ, "MS605_DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ms605.cli.cli", "gui", "--sim", "3", "--speed", str(speed), "--port", "0"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line.rstrip("\n")) for line in proc.stdout], daemon=True).start()
    match = FIRST_LINE.match(lines.get(timeout=20))
    assert match, "first line must be the URL"
    return proc, match.group(2), match.group(3)


def _tls_context(tmp_path: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=tmp_path / "cal_results" / "gui_tls" / "ca.pem")
    # The sync client's threaded SSL I/O intermittently stalls TLS 1.3 handshakes
    # on macOS. Keep these application-flow tests on TLS 1.2; the async-client
    # test below covers TLS 1.3 against the unchanged production server config.
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


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


@pytest.mark.timeout(30)
def test_gui_tls13_websocket_with_async_client(tmp_path):
    proc, port, token = _launch(tmp_path)
    try:
        tls = ssl.create_default_context(cafile=tmp_path / "cal_results" / "gui_tls" / "ca.pem")
        tls.minimum_version = tls.maximum_version = ssl.TLSVersion.TLSv1_3
        auth = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", headers=auth, verify=tls) as http:
            _wait_healthy(http)

        async def check():
            async with contextlib.AsyncExitStack() as stack:
                for _ in range(3):
                    ws = await stack.enter_async_context(
                        async_connect(f"wss://127.0.0.1:{port}/ws", additional_headers=auth, ssl=tls)
                    )
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert message["type"] == "snapshot"
                    assert message["data"]["sensors"] == []

        asyncio.run(check())
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) in (0, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.timeout(60)
def test_gui_end_to_end(tmp_path):
    proc, port, token = _launch(tmp_path)
    try:
        base = f"https://127.0.0.1:{port}"
        auth = {"Authorization": f"Bearer {token}"}
        tls = _tls_context(tmp_path)

        with httpx.Client(base_url=base, headers=auth, timeout=10, verify=tls) as http:
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

            url = f"wss://127.0.0.1:{port}/ws"
            with connect(url, additional_headers=auth, ssl=tls) as ws_a, connect(
                url, additional_headers=auth, ssl=tls
            ) as ws_b:
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
        tls = _tls_context(tmp_path)
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", headers=auth, timeout=10, verify=tls) as http:
            _wait_healthy(http)
            with connect(f"wss://127.0.0.1:{port}/ws", additional_headers=auth, ssl=tls) as ws:
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


def _jobs(message: dict) -> dict[str, dict]:
    return {j["device_id"]: j for j in message["data"]["jobs"]}


def _batch(state: str, round_: int = 1):
    return lambda m: m["type"] == "batch" and m["data"]["state"] == state and m["data"]["round"] == round_


def _learning(device_id: str):
    def match(m: dict) -> bool:
        if m["type"] == "calibration_job":
            return m["data"]["device_id"] == device_id and m["data"]["state"] == "learning"
        return m["type"] == "batch" and _jobs(m)[device_id]["state"] == "learning"

    return match


def _live_by_sensor(messages: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in messages:
        if m["type"] == "live":
            out.setdefault(m["data"]["device_id"], []).append(m)
    return out


@pytest.mark.timeout(60)
def test_gui_calibration_end_to_end(tmp_path):
    """M3: throttled live for the watched sensors only and off after unsubscribe, preflight, a countdown
    batch every screen follows (a late joiner included), a sensor dropped mid-learning, a retry of only
    that sensor, before/after values, the history (docs/GUI_API.md 14.9.3)."""
    proc, port, token = _launch(tmp_path, speed=40)  # calibration 4.5 s, keep-alive 0.375 s, button window 3 s
    try:
        auth = {"Authorization": f"Bearer {token}"}
        ids = [SIM1, SIM2, SIM3]
        tls = _tls_context(tmp_path)
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", headers=auth, timeout=10, verify=tls) as http:
            _wait_healthy(http)
            url = f"wss://127.0.0.1:{port}/ws"
            with contextlib.ExitStack() as stack:
                ws_a = stack.enter_context(connect(url, additional_headers=auth, ssl=tls))
                ws_b = stack.enter_context(connect(url, additional_headers=auth, ssl=tls))
                a, b = Reader(ws_a), Reader(ws_b)
                for reader in (a, b):
                    assert reader.until(lambda m: True)["type"] == "snapshot"

                assert http.post("/api/gather/start").status_code == 200
                for index in (1, 2, 3):
                    assert http.post(f"/api/sim/press/{index}").status_code == 204
                for device_id in ids:
                    a.until(_link(device_id, "connected"))

                # the monitor on two sensors: 4 Hz at most although the simulator pushes 40 Hz
                ws_a.send(json.dumps({"type": "live_subscribe", "data": {"device_ids": [SIM1, SIM2]}}))
                live = a.until(lambda m: m["type"] == "live")
                assert live["seq"] is None and live["data"]["device_id"] in (SIM1, SIM2)
                assert len(live["data"]["zones"]) == 7 and live["data"]["zones"][0]["distance_m"] == 0.8
                window = 1.5
                frames = _live_by_sensor([live, *a.drain(window)])
                assert sorted(frames) == [SIM1, SIM2]  # SIM3 is not watched
                for device_id, got in frames.items():
                    assert 3 <= len(got) <= window / 0.25 + 2, (device_id, len(got))
                    assert all(m["seq"] is None for m in got)
                    gaps = [y["ts"] - x["ts"] for x, y in zip(got, got[1:], strict=False)]
                    assert min(gaps) >= 0.2, gaps

                # unsubscribed: the output stops (frames already in flight are allowed for one interval)
                ws_a.send(json.dumps({"type": "live_unsubscribe", "data": {"device_ids": [SIM1, SIM2]}}))
                a.drain(0.5)
                assert not [m for m in a.drain(1.0) if m["type"] == "live"]

                preflight = http.post("/api/preflight", json={"device_ids": ids, "window_s": 0.5})
                assert preflight.status_code == 200
                results = preflight.json()["results"]
                assert [r["device_id"] for r in results] == ids and all(r["occupied"] is False for r in results)

                created = http.post("/api/batches", json={"device_ids": ids, "start": "delay", "delay_s": 1})
                assert created.status_code == 202
                batch_id = created.json()["batch_id"]
                for reader in (a, b):
                    reader.until(_batch("waiting"))
                    countdown = reader.until(lambda m: m["type"] == "countdown")
                    assert countdown["seq"] is None and countdown["data"]["batch_id"] == batch_id

                # a third screen opens mid-batch: its snapshot already holds the batch
                ws_c = stack.enter_context(connect(url, additional_headers=auth, ssl=tls))
                c = Reader(ws_c)
                snapshot = c.until(lambda m: True)
                assert snapshot["type"] == "snapshot"
                joined = snapshot["data"]["batch"]
                assert joined["batch_id"] == batch_id and joined["round"] == 1
                assert joined["state"] in ("waiting", "running") and joined["device_ids"] == ids
                assert [j["device_id"] for j in joined["jobs"]] == ids

                a.until(_batch("running"))
                a.until(lambda m: m["type"] == "gather" and m["data"]["gathering"] is False)  # G20

                a.until(_learning(SIM2))
                assert http.post("/api/sim/drop/2").status_code == 204
                jobs = _jobs(a.until(_batch("done")))
                for device_id in (SIM1, SIM3):
                    job = jobs[device_id]
                    assert job["state"] == "succeeded" and job["history_saved"] is True
                    for side in ("before", "after"):  # thresholds are tag51 pairs, one per zone
                        assert len(job[side]) == 7
                        assert all(set(pair) == {"trigger", "maintain"} for pair in job[side])
                lost = jobs[SIM2]
                assert (lost["state"], lost["started"], lost["retryable"]) == ("lost", True, True)

                retry = http.post(f"/api/batches/{batch_id}/retry", json={})
                assert retry.status_code == 409 and retry.json()["error"]["code"] == "not_connected"
                assert http.post("/api/gather/start").status_code == 200
                assert http.post("/api/sim/press/2").status_code == 204
                a.until(_link(SIM2, "connected"))

                retried = http.post(f"/api/batches/{batch_id}/retry", json={"start": "now"})
                assert retried.status_code == 202 and retried.json()["round"] == 2
                final = _jobs(a.until(_batch("done", round_=2)))
                assert final[SIM2]["state"] == "succeeded" and final[SIM2]["attempt"] == 2
                assert final[SIM2]["before"] and final[SIM2]["after"] and final[SIM2]["history_saved"] is True
                assert final[SIM1] == jobs[SIM1] and final[SIM3] == jobs[SIM3]

                history = (tmp_path / "cal_results" / "sim" / "calibration_history.jsonl").read_text("utf-8")
                lines = [json.loads(line) for line in history.splitlines()]
                assert sorted(line["device_id"] for line in lines) == ids
                assert all(len(line["zones"]) == 7 for line in lines)

                ordered_a = [(m["seq"], m["type"]) for m in a.log if m["seq"] is not None]
                for reader in (b, c):
                    reader.until(lambda m: m["seq"] is not None and m["seq"] >= ordered_a[-1][0])
                ordered_b = [(m["seq"], m["type"]) for m in b.log if m["seq"] is not None]
                assert ordered_a == ordered_b  # the phone and the laptop saw the same batch (D11)
                seqs = [seq for seq, _ in ordered_a]
                assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
                from_snapshot = [(m["seq"], m["type"]) for m in c.log[1:] if m["seq"] is not None]
                assert from_snapshot == [x for x in ordered_a if x[0] > snapshot["seq"]]  # the late joiner too
                assert not [m for m in b.log + c.log if m["type"] == "live"]  # only A ever subscribed

        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) in (0, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _apply_done(apply_id: str):
    return lambda m: m["type"] == "apply" and m["data"]["apply_id"] == apply_id and m["data"]["state"] == "done"


def _revs(http, *device_ids: str) -> dict[str, int]:
    """expect_rev as a fresh preview would give it: each sensor's config_rev now."""
    sensors = {s["device_id"]: s for s in http.get("/api/state").json()["sensors"]}
    return {i: sensors[i]["config_rev"] for i in device_ids}


@pytest.mark.timeout(60)
def test_gui_editing_end_to_end(tmp_path):
    """M4: edit -> preview -> apply -> rollback over HTTP + WS on a real process; two screens
    follow the same job (docs/GUI_API.md 15.10.3)."""
    proc, port, token = _launch(tmp_path, speed=40)
    try:
        auth = {"Authorization": f"Bearer {token}"}
        ids = [SIM1, SIM2, SIM3]
        tls = _tls_context(tmp_path)
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", headers=auth, timeout=10, verify=tls) as http:
            _wait_healthy(http)
            url = f"wss://127.0.0.1:{port}/ws"
            with connect(url, additional_headers=auth, ssl=tls) as ws_a, connect(
                url, additional_headers=auth, ssl=tls
            ) as ws_b:
                a, b = Reader(ws_a), Reader(ws_b)
                for reader in (a, b):
                    assert reader.until(lambda m: True)["type"] == "snapshot"
                assert http.post("/api/gather/start").status_code == 200
                for index in (1, 2, 3):
                    assert http.post(f"/api/sim/press/{index}").status_code == 204
                for device_id in ids:
                    a.until(_link(device_id, "connected"))

                config = http.get(f"/api/sensors/{SIM1}/config").json()
                assert config["profile"]["sensitivity"] == 2 and config["config_rev"] == 0
                assert config["profile"]["zone_thresholds"][0]["trigger"] == 95

                edit = {"zone_thresholds": {"mode": "relative", "trigger": [5] * 7, "maintain": [None] * 7}}
                preview = http.post("/api/drafts/preview", json={"targets": ids, "changes": edit})
                assert preview.status_code == 200
                items = preview.json()["items"]
                assert all(i["after"]["zone_thresholds"][0]["trigger"] == 100 and i["risks"] == [] for i in items)

                expect = {i["device_id"]: i["config_rev"] for i in items}
                created = http.post("/api/apply", json={"targets": ids, "changes": edit, "expect_rev": expect})
                assert created.status_code == 202
                apply_id = created.json()["apply_id"]
                done = [reader.until(_apply_done(apply_id), timeout=20)["data"] for reader in (a, b)]
                assert done[0] == done[1] and [i["state"] for i in done[0]["items"]] == ["verified"] * 3

                assert http.post("/api/sim/drop/3").status_code == 204
                a.until(_link(SIM3, "lost"))
                refused = http.post(
                    "/api/apply", json={"targets": [SIM1, SIM3], "changes": edit, "expect_rev": _revs(http, SIM1, SIM3)}
                )
                assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_connected"

                snapshot = done[0]["items"][0]["snapshot"]
                back = {"items": [{"device_id": SIM1, "snapshot": snapshot}], "expect_rev": _revs(http, SIM1)}
                undo = http.post("/api/rollback", json=back)
                assert undo.status_code == 202
                undone = a.until(_apply_done(undo.json()["apply_id"]), timeout=20)["data"]
                assert undone["kind"] == "rollback" and undone["items"][0]["state"] == "verified"
                assert http.get(f"/api/sensors/{SIM1}/config").json()["profile"]["zone_thresholds"][0]["trigger"] == 95

                snapshots = http.get(f"/api/sensors/{SIM1}/snapshots").json()["snapshots"]
                assert [s["reason"] for s in snapshots] == ["rollback", "apply"]

                synced = http.post("/api/time-sync", json={"device_ids": [SIM1, SIM2]})
                assert synced.status_code == 200 and all(i["written_at"] for i in synced.json()["items"])

                assert http.get(f"/api/sensors/{SIM1}/history").json()["records"] == []
                device = http.get(f"/api/sensors/{SIM1}/device-history", params={"kind": "presence"})
                assert device.status_code == 200 and device.json()["presence"] == []

                ordered_a = [(m["seq"], m["type"]) for m in a.log if m["seq"] is not None]
                b.until(lambda m: m["seq"] is not None and m["seq"] >= ordered_a[-1][0])
                ordered_b = [(m["seq"], m["type"]) for m in b.log if m["seq"] is not None]
                assert ordered_a == ordered_b[: len(ordered_a)]  # the same messages in the same order (D11)
                seqs = [seq for seq, _ in ordered_a]
                assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) in (0, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.timeout(90)
def test_gui_editing_flows_end_to_end(tmp_path):
    """M4 integration: one sensor's draft, a failure in the middle of a bulk apply (a link dropped mid-job),
    rollback of the rest, clone, a calibration that refuses edits, and the history list it fills."""
    proc, port, token = _launch(tmp_path, speed=40)
    try:
        auth = {"Authorization": f"Bearer {token}"}
        ids = [SIM1, SIM2, SIM3]
        tls = _tls_context(tmp_path)
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", headers=auth, timeout=10, verify=tls) as http:
            _wait_healthy(http)
            with connect(f"wss://127.0.0.1:{port}/ws", additional_headers=auth, ssl=tls) as ws:
                reader = Reader(ws)
                assert reader.until(lambda m: True)["type"] == "snapshot"

                def connect_all(*indexes: int) -> None:
                    assert http.post("/api/gather/start").status_code == 200
                    for index in indexes:
                        assert http.post(f"/api/sim/press/{index}").status_code == 204
                    for index in indexes:
                        reader.until(_link(ids[index - 1], "connected"))

                def thresholds(device_id: str) -> list[dict]:
                    return http.get(f"/api/sensors/{device_id}/config").json()["profile"]["zone_thresholds"]

                connect_all(1, 2, 3)

                # one sensor: absolute draft -> preview -> apply -> verified
                edit = {"sensitivity": 3}
                preview = http.post("/api/drafts/preview", json={"targets": [SIM1], "changes": edit}).json()
                (item,) = preview["items"]
                assert item["after"]["sensitivity"] == 3 and item["risks"] == ["sensitivity_only"]
                body = {"targets": [SIM1], "changes": edit, "expect_rev": {SIM1: item["config_rev"]}}
                created = http.post("/api/apply", json=body)
                assert created.status_code == 202
                done = reader.until(_apply_done(created.json()["apply_id"]), timeout=20)["data"]
                assert [i["state"] for i in done["items"]] == ["verified"]
                assert http.get(f"/api/sensors/{SIM1}/config").json()["profile"]["sensitivity"] == 3
                stale = http.post("/api/apply", json=body)  # the preview's config_rev is behind now
                assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale"

                # a bulk apply loses the third sensor in the middle of the job: it fails, the rest verify
                before = {i: thresholds(i) for i in ids}
                edit = {"zone_thresholds": {"mode": "relative", "trigger": [5] * 7, "maintain": [None] * 7}}
                assert http.post("/api/gather/stop").status_code == 200  # or it would reconnect the dropped link
                bulk = {"targets": ids, "changes": edit, "expect_rev": _revs(http, *ids)}
                created = http.post("/api/apply", json=bulk)
                assert created.status_code == 202
                assert http.post("/api/sim/drop/3").status_code == 204  # each item takes ~0.2 s at speed 40
                final = reader.until(_apply_done(created.json()["apply_id"]), timeout=20)["data"]
                states = {i["device_id"]: i for i in final["items"]}
                assert [states[i]["state"] for i in ids] == ["verified", "verified", "failed"]
                assert states[SIM3]["error"]
                for device_id in (SIM1, SIM2):
                    assert thresholds(device_id)[0]["trigger"] == before[device_id][0]["trigger"] + 5

                # rollback of what was applied (a failed item before its snapshot has none)
                items = [{"device_id": i, "snapshot": states[i]["snapshot"]} for i in (SIM1, SIM2)]
                undo = http.post("/api/rollback", json={"items": items, "expect_rev": _revs(http, SIM1, SIM2)})
                assert undo.status_code == 202
                undone = reader.until(_apply_done(undo.json()["apply_id"]), timeout=20)["data"]
                assert [i["state"] for i in undone["items"]] == ["verified", "verified"]
                assert {i: thresholds(i) for i in (SIM1, SIM2)} == {i: before[i] for i in (SIM1, SIM2)}

                # clone SIM1's sensitivity (3, set above) onto the others, SIM3 after it reconnects
                connect_all(3)
                body = {"source": SIM1, "targets": [SIM2, SIM3], "sections": ["sensitivity"]}
                seen = http.post("/api/clone/preview", json=body).json()
                assert seen["kind"] == "clone"
                expect = {**{i["device_id"]: i["config_rev"] for i in seen["items"]}, SIM1: seen["source_rev"]}
                created = http.post("/api/clone", json={**body, "expect_rev": expect})
                assert created.status_code == 202
                cloned = reader.until(_apply_done(created.json()["apply_id"]), timeout=20)["data"]
                assert cloned["source"] == SIM1 and [i["state"] for i in cloned["items"]] == ["verified"] * 2
                for device_id in (SIM2, SIM3):
                    assert http.get(f"/api/sensors/{device_id}/config").json()["profile"]["sensitivity"] == 3

                # a calibrating sensor refuses edits and the other sensors do not
                assert http.get(f"/api/sensors/{SIM1}/history").json()["records"] == []
                rev = http.get(f"/api/sensors/{SIM1}/config").json()["config_rev"]
                started = http.post("/api/batches", json={"device_ids": [SIM1]})
                assert started.status_code == 202
                reader.until(_batch("running"))
                again = {"targets": [SIM1], "changes": {"sensitivity": 2}, "expect_rev": _revs(http, SIM1)}
                refused = http.post("/api/apply", json=again)
                assert refused.status_code == 409 and refused.json()["error"]["code"] == "batch_active"
                other = http.post("/api/drafts/preview", json={"targets": [SIM2], "changes": {"sensitivity": 2}})
                assert other.status_code == 200
                reader.until(_batch("done"))

                records = http.get(f"/api/sensors/{SIM1}/history").json()["records"]
                assert len(records) == 1 and len(records[0]["zones"]) == 7
                assert http.get(f"/api/sensors/{SIM1}/config").json()["config_rev"] > rev

        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) in (0, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
