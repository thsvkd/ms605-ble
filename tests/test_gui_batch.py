"""ms605 gui multi-sensor calibration: the server's one batch over REST + WS
(create / countdown / cancel / retry of the failed ones / late joiners), the
operation lock on release and preflight, request validation, the preflight
presence warning, and the "identify" wait (docs/GUI_API.md 14.5, 14.6.5-14.6.7,
14.9.1). Every value is synthetic."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import recv_until

import ms605.fleet as fleet_mod
from ms605.calibration import CalibrationJob
from ms605.events import LinkState
from ms605.protocol import TAG_DETECT_MODE
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS

pytestmark = pytest.mark.timeout(60)

SIM1, SIM2, SIM3 = (f"53494d36303500{i:02x}" for i in (1, 2, 3))
LEARNED = [{"trigger": t, "maintain": m} for t, m in DEFAULT_LEARNED_THRESHOLDS]
DONE = ("done", "cancelled")


@pytest.fixture
def rig(make_gui, monkeypatch):
    """make_gui() whose sensors learn in 0.6 s (timeout 2 s at speed 100) and report progress every 0.1 s."""
    monkeypatch.setitem(CalibrationJob.__init__.__kwdefaults__, "progress_interval", 0.1)

    def make(count: int = 3):
        gui = make_gui(sim_count=count)
        for dev in gui.sim.devices:
            dev.calibration_secs = 60
        return gui

    return make


def _id(index: int) -> str:
    return f"53494d36303500{index:02x}"


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def _gather(gui, count: int) -> list[str]:
    gui.post("/api/gather/start")
    gui.post("/api/sim/press-all")
    ids = [_id(i) for i in range(1, count + 1)]
    _wait(lambda: all(i in gui.fleet.sessions and gui.fleet.sessions[i].state is LinkState.CONNECTED for i in ids))
    gui.post("/api/gather/stop")
    return ids


def _batch(pred=lambda d: True):
    return lambda m: m["type"] == "batch" and pred(m["data"])


def _job(device_id: str, pred=lambda d: True):
    def match(m: dict) -> bool:
        if m["type"] == "calibration_job" and m["data"]["device_id"] == device_id:
            return pred(m["data"])
        if m["type"] == "batch":
            return any(j["device_id"] == device_id and pred(j) for j in m["data"]["jobs"])
        return False

    return match


def _until(ws, predicate) -> list[dict]:
    return recv_until(ws, predicate, limit=5000)


def _jobs(batch: dict) -> dict[str, dict]:
    return {j["device_id"]: j for j in batch["jobs"]}


def _states(messages: list[dict], device_id: str) -> list[str]:
    """The job states one client saw for `device_id`, consecutive repeats removed."""
    seen: list[str] = []
    for m in messages:
        if m["type"] == "calibration_job" and m["data"]["device_id"] == device_id:
            state = m["data"]["state"]
        elif m["type"] == "batch":
            state = _jobs(m["data"])[device_id]["state"]
        else:
            continue
        if not seen or seen[-1] != state:
            seen.append(state)
    return seen


def _is_subsequence(part: list[str], whole: list[str]) -> bool:
    it = iter(whole)
    return all(x in it for x in part)


def _ordered(messages: list[dict]) -> list[tuple]:
    return [(m["seq"], m["type"], m["data"]) for m in messages if m["seq"] is not None]


def _tag52_writes(dev) -> int:
    return sum(1 for f in dev.frames_in if any(tag == TAG_DETECT_MODE for tag, _ in f.attributes))


# -- the whole flow ----------------------------------------------------------------------


def test_batch_lifecycle(rig):
    gui = rig(3)
    ids = _gather(gui, 3)
    with gui.ws() as ws:
        ws.receive_json()
        created = gui.post("/api/batches", {"device_ids": ids, "start": "now"})
        assert created.status_code == 202
        view = created.json()
        assert view["state"] in ("waiting", "running") and view["round"] == 1 and view["start"] == "now"
        assert view["device_ids"] == view["round_ids"] == ids
        assert view["expected_s"] == pytest.approx(1.8)  # 180 s / speed 100: not device progress
        assert [j["attempt"] for j in view["jobs"]] == [1, 1, 1]
        got = _until(ws, _batch(lambda d: d["state"] == "done"))
        first_batch = next(m for m in got if m["type"] == "batch")
        assert first_batch["data"]["batch_id"] == view["batch_id"]
        assert first_batch["data"]["state"] in ("waiting", "running")
        for device_id in ids:
            assert _is_subsequence(["starting", "learning", "succeeded"], _states(got, device_id)), device_id
            elapsed = [
                m["data"]["elapsed_s"]
                for m in got
                if m["type"] == "calibration_job" and m["data"]["device_id"] == device_id and m["data"]["elapsed_s"]
            ]
            assert len(elapsed) >= 2 and elapsed == sorted(elapsed)
        final = got[-1]["data"]
        for job in final["jobs"]:
            assert job["state"] == "succeeded" and job["started"] and job["history_saved"] and not job["retryable"]
            assert len(job["before"]) == 7 and job["after"] == LEARNED
    lines = gui.storage.history_path.read_text("utf-8").splitlines()
    assert sorted(json.loads(line)["device_id"] for line in lines) == ids
    state = gui.get("/api/state").json()
    assert all(s["last_calibration"] is not None for s in state["sensors"])
    assert state["batch"]["state"] == "done" and state["batch"]["batch_id"] == view["batch_id"]
    assert gui.get(f"/api/batches/{view['batch_id']}").json() == state["batch"]


def test_seven_sensors_one_dropped_while_learning(rig):
    """M3 done criterion: 1 of 7 dropped mid-calibration, the other 6 succeed; two screens see the same."""
    gui = rig(7)
    ids = _gather(gui, 7)
    with gui.ws() as a, gui.ws() as b:
        a.receive_json()
        b.receive_json()
        assert gui.post("/api/batches", {"device_ids": ids}).status_code == 202
        got_a = _until(a, _job(_id(4), lambda j: j["state"] == "learning"))
        gui.post("/api/sim/drop/4")
        got_a += _until(a, _batch(lambda d: d["state"] == "done"))
        jobs = _jobs(got_a[-1]["data"])
        assert jobs[_id(4)]["state"] == "lost" and jobs[_id(4)]["started"] and jobs[_id(4)]["retryable"]
        assert all(jobs[i]["state"] == "succeeded" for i in ids if i != _id(4))
        last = got_a[-1]["seq"]
        got_b = _until(b, lambda m: m["seq"] == last)
        assert _ordered(got_a) == _ordered(got_b)


def test_retry_only_the_lost_sensor(rig):
    gui = rig(3)
    ids = _gather(gui, 3)
    with gui.ws() as ws:
        ws.receive_json()
        batch_id = gui.post("/api/batches", {"device_ids": ids}).json()["batch_id"]
        _until(ws, _job(SIM2, lambda j: j["state"] == "learning"))
        gui.post("/api/sim/drop/2")
        first = _until(ws, _batch(lambda d: d["state"] == "done"))[-1]["data"]
        assert _jobs(first)[SIM2]["state"] == "lost"

        retry = gui.post(f"/api/batches/{batch_id}/retry", {})
        assert retry.status_code == 409 and retry.json()["error"]["code"] == "not_connected"
        wrong = gui.post(f"/api/batches/{batch_id}/retry", {"device_ids": [SIM1]})
        assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "invalid"

        gui.post("/api/gather/start")
        gui.post("/api/sim/press/2")
        _wait(lambda: gui.fleet.sessions[SIM2].state is LinkState.CONNECTED)
        retried = gui.post(f"/api/batches/{batch_id}/retry", {"start": "now"})
        assert retried.status_code == 202
        view = retried.json()
        assert view["batch_id"] == batch_id and view["round"] == 2 and view["round_ids"] == [SIM2]
        assert view["device_ids"] == ids
        final = _until(ws, _batch(lambda d: d["round"] == 2 and d["state"] == "done"))[-1]["data"]
        jobs = _jobs(final)
        assert jobs[SIM2]["state"] == "succeeded" and jobs[SIM2]["attempt"] == 2
        for device_id in (SIM1, SIM3):  # not retried: kept as they were
            assert jobs[device_id] == _jobs(first)[device_id] and jobs[device_id]["attempt"] == 1


def test_gathering_stops_when_the_batch_fires(rig):
    gui = rig(2)
    ids = _gather(gui, 2)
    gui.post("/api/gather/start")
    with gui.ws() as ws:
        assert ws.receive_json()["data"]["gather"]["gathering"] is True
        gui.post("/api/batches", {"device_ids": ids})
        got = _until(ws, lambda m: m["type"] == "gather" and not m["data"]["gathering"])
        running = next(i for i, m in enumerate(got) if m["type"] == "batch" and m["data"]["state"] == "running")
        assert running < len(got) - 1  # stopped after the round went RUNNING (G20)
        assert not gui.fleet.gathering
        _until(ws, _batch(lambda d: d["state"] == "done"))


def test_countdown_then_cancel_needs_no_device_io(rig):
    gui = rig(2)
    ids = _gather(gui, 2)
    with gui.ws() as ws:
        ws.receive_json()
        created = gui.post("/api/batches", {"device_ids": ids, "start": "delay", "delay_s": 5})
        assert created.status_code == 202 and created.json()["state"] == "waiting"
        batch_id = created.json()["batch_id"]
        countdowns = [m for m in _until(ws, lambda m: m["type"] == "countdown") if m["type"] == "countdown"]
        countdowns += [m for m in _until(ws, lambda m: m["type"] == "countdown") if m["type"] == "countdown"]
        first, second = countdowns[0], countdowns[-1]
        assert first["seq"] is None and first["data"]["batch_id"] == batch_id
        assert 0 < second["data"]["remaining_s"] < first["data"]["remaining_s"] <= 5
        writes = [_tag52_writes(dev) for dev in gui.sim.devices]

        cancelled = gui.post(f"/api/batches/{batch_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
        final = _until(ws, _batch(lambda d: d["state"] == "cancelled"))[-1]["data"]
        assert all(j["state"] == "cancelled" and not j["started"] for j in final["jobs"])
        assert [_tag52_writes(dev) for dev in gui.sim.devices] == writes  # no tag52 write
        assert all(gui.fleet.sessions[i].state is LinkState.CONNECTED for i in ids)
        assert gui.post(f"/api/batches/{batch_id}/cancel").status_code == 200  # idempotent


def test_cancel_while_running_drops_the_link(rig):
    gui = rig(1)
    (device_id,) = _gather(gui, 1)
    with gui.ws() as ws:
        ws.receive_json()
        batch_id = gui.post("/api/batches", {"device_ids": [device_id]}).json()["batch_id"]
        _until(ws, _job(device_id, lambda j: j["state"] == "learning"))
        cancelled = gui.post(f"/api/batches/{batch_id}/cancel")
        assert cancelled.status_code == 200
        job = cancelled.json()["jobs"][0]
        assert cancelled.json()["state"] == "cancelled" and job["state"] == "cancelled" and job["started"]
        assert gui.fleet.sessions[device_id].state is LinkState.DISCONNECTED


def test_scheduled_start_with_a_fast_clock(rig, monkeypatch):
    offset = [0.0]
    monkeypatch.setattr(fleet_mod, "time", SimpleNamespace(time=lambda: time.time() + offset[0]))
    gui = rig(1)
    (device_id,) = _gather(gui, 1)
    with gui.ws() as ws:
        ws.receive_json()
        at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        created = gui.post("/api/batches", {"device_ids": [device_id], "start": "at", "at": at})
        assert created.status_code == 202 and created.json()["start"] == "at"
        time.sleep(1.0)  # far longer than the 0.3 s idle drop: keep-alive holds the link
        assert gui.get("/api/state").json()["batch"]["state"] == "waiting"
        assert gui.fleet.sessions[device_id].state is LinkState.CONNECTED
        offset[0] = 3600.0
        started = time.monotonic()
        _until(ws, _batch(lambda d: d["state"] == "running"))
        assert time.monotonic() - started < 2.0  # the core re-reads the clock every second
        final = _until(ws, _batch(lambda d: d["state"] == "done"))[-1]["data"]
        assert final["jobs"][0]["state"] == "succeeded"


def test_a_late_joiner_sees_the_running_batch(rig):
    gui = rig(3)
    ids = _gather(gui, 3)
    with gui.ws() as a:
        a.receive_json()
        gui.post("/api/batches", {"device_ids": ids})
        for device_id in ids:
            _until(a, _job(device_id, lambda j: j["state"] == "learning"))
        with gui.ws() as late:
            snapshot = late.receive_json()
            batch = snapshot["data"]["batch"]
            assert batch["state"] == "running"
            assert [(j["state"], j["started"], j["attempt"]) for j in batch["jobs"]] == [("learning", True, 1)] * 3
            got = _until(late, _batch(lambda d: d["state"] == "done"))
            seqs = [m["seq"] for m in got if m["seq"] is not None]
            assert seqs == list(range(snapshot["seq"] + 1, snapshot["seq"] + 1 + len(seqs)))


def test_one_batch_at_a_time_and_the_operation_lock(rig):
    gui = rig(2)
    ids = _gather(gui, 2)
    with gui.ws() as ws:
        ws.receive_json()
        batch_id = gui.post("/api/batches", {"device_ids": [SIM1]}).json()["batch_id"]
        _until(ws, _job(SIM1, lambda j: j["state"] == "learning"))

        second = gui.post("/api/batches", {"device_ids": [SIM2]})
        assert second.status_code == 409 and second.json()["error"]["code"] == "batch_active"
        retry = gui.post(f"/api/batches/{batch_id}/retry", {})
        assert retry.status_code == 409 and retry.json()["error"]["code"] == "batch_active"
        release = gui.post("/api/release", {"device_ids": [SIM1]})
        assert release.status_code == 409 and release.json()["error"]["code"] == "batch_active"
        assert gui.post("/api/release", {}).status_code == 409
        assert gui.fleet.sessions[SIM1].state is LinkState.CONNECTED  # the link is kept
        preflight = gui.post("/api/preflight", {"device_ids": [SIM1], "window_s": 0.3})
        assert preflight.status_code == 409 and preflight.json()["error"]["code"] == "busy"
        sensor = next(s for s in gui.get("/api/state").json()["sensors"] if s["device_id"] == SIM1)
        assert sensor["live"]["busy"] == "calibration"  # every screen shows the lock

        _until(ws, _batch(lambda d: d["state"] == "done"))
        assert gui.post("/api/release", {"device_ids": [SIM2]}).status_code == 204
    assert ids


@pytest.mark.parametrize("release", [{}, {"device_ids": [SIM1]}], ids=["all", "one"])
def test_a_batch_requested_while_a_release_closes_links_is_checked_after_it(rig, release):
    """G22: release and batch creation never interleave. A batch that came in while the release
    awaited (stop_gather, connect cancels, the closes) is checked after it, so it never passes its
    checks and then loses its links."""
    gui = rig(2)
    _gather(gui, 2)
    gui.post("/api/gather/start")  # release-all awaits stop_gather() first
    real_release = gui.fleet.release
    releasing = threading.Event()

    async def slow_release(device_ids=None):
        releasing.set()
        await asyncio.sleep(0.3)  # a slow BLE disconnect: every session is still CONNECTED meanwhile
        await real_release(device_ids)

    gui.fleet.release = slow_release
    with ThreadPoolExecutor(1) as pool:
        released = pool.submit(gui.post, "/api/release", release)
        assert releasing.wait(5)
        created = gui.post("/api/batches", {"device_ids": [SIM1], "start": "now"})
        assert released.result().status_code == 204
    assert created.status_code == 404 and created.json()["error"]["code"] == "not_found"
    assert gui.get("/api/state").json()["batch"] is None


def test_cancelling_a_retry_countdown_keeps_the_sensor_retryable(rig):
    """A retry round cancelled before it started a sensor did not happen for it: the earlier
    lost result, its attempt and the retry stay (14.5.5)."""
    gui = rig(2)
    ids = _gather(gui, 2)
    with gui.ws() as ws:
        ws.receive_json()
        batch_id = gui.post("/api/batches", {"device_ids": ids}).json()["batch_id"]
        _until(ws, _job(SIM2, lambda j: j["state"] == "learning"))
        gui.post("/api/sim/drop/2")
        lost = _jobs(_until(ws, _batch(lambda d: d["state"] == "done"))[-1]["data"])[SIM2]
        assert (lost["state"], lost["attempt"], lost["retryable"]) == ("lost", 1, True)
        gui.post("/api/gather/start")
        gui.post("/api/sim/press/2")
        _wait(lambda: gui.fleet.sessions[SIM2].state is LinkState.CONNECTED)

        waiting = gui.post(f"/api/batches/{batch_id}/retry", {"start": "delay", "delay_s": 30})
        assert waiting.status_code == 202 and _jobs(waiting.json())[SIM2]["attempt"] == 2
        cancelled = gui.post(f"/api/batches/{batch_id}/cancel").json()
        assert (cancelled["state"], cancelled["round"], cancelled["round_ids"]) == ("cancelled", 2, [SIM2])
        assert _jobs(cancelled)[SIM2] == lost
        shown = _until(ws, _batch(lambda d: d["state"] == "cancelled"))[-1]["data"]
        assert _jobs(shown)[SIM2] == lost  # every screen keeps the retry panel

        again = gui.post(f"/api/batches/{batch_id}/retry", {"start": "now"})
        assert again.status_code == 202 and again.json()["round"] == 3
        final = _jobs(_until(ws, _batch(lambda d: d["round"] == 3 and d["state"] == "done"))[-1]["data"])
        assert (final[SIM2]["state"], final[SIM2]["attempt"]) == ("succeeded", 2)


# -- validation ----------------------------------------------------------------------------


def test_request_validation(rig):
    gui = rig(2)
    _gather(gui, 2)
    soon = datetime.now(timezone.utc) + timedelta(minutes=5)
    bad = [
        {"device_ids": [SIM1], "start": "delay"},
        {"device_ids": [SIM1], "start": "now", "delay_s": 5},
        {"device_ids": [SIM1], "start": "at", "at": soon.replace(tzinfo=None).isoformat()},
        {"device_ids": [SIM1, SIM1]},
        {"device_ids": []},
        {"device_ids": [SIM1], "colour": "red"},
    ]
    for body in bad:
        response = gui.post("/api/batches", body)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request", body
    for at in (datetime.now(timezone.utc) - timedelta(minutes=1), datetime.now(timezone.utc) + timedelta(hours=25)):
        response = gui.post("/api/batches", {"device_ids": [SIM1], "start": "at", "at": at.isoformat()})
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid", at

    missing = gui.post("/api/batches", {"device_ids": [SIM1, "0123"]})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    gui.post("/api/sim/drop/2")
    _wait(lambda: gui.fleet.sessions[SIM2].state is LinkState.LOST)
    offline = gui.post("/api/batches", {"device_ids": [SIM1, SIM2]})
    assert offline.status_code == 409 and offline.json()["error"]["code"] == "not_connected"
    assert gui.get("/api/state").json()["batch"] is None  # nothing was created

    assert gui.get("/api/batches/0123").status_code == 404
    batch_id = gui.post("/api/batches", {"device_ids": [SIM1], "start": "delay", "delay_s": 60}).json()["batch_id"]
    assert gui.get("/api/batches/0123").status_code == 404
    assert gui.post("/api/batches/0123/cancel").status_code == 404
    assert gui.get(f"/api/batches/{batch_id}").json()["state"] == "waiting"
    assert gui.post(f"/api/batches/{batch_id}/cancel").status_code == 200


def test_preflight_reports_presence_in_request_order(rig):
    gui = rig(3)
    ids = _gather(gui, 3)
    gui.sim.devices[1].signal = [(5000, 5000)] * 7  # someone near sensor 2
    gui.post("/api/sim/drop/3")
    _wait(lambda: gui.fleet.sessions[SIM3].state is LinkState.LOST)
    order = [SIM3, SIM2, SIM1]
    response = gui.post("/api/preflight", {"device_ids": order, "window_s": 0.3})
    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["device_id"] for r in results] == order
    lost, occupied, empty = results
    assert lost["error"] == "not connected" and lost["occupied"] is None and lost["samples"] == 0
    assert occupied["occupied"] is True and occupied["presence"] is True
    assert empty["occupied"] is False and empty["presence"] is False and empty["samples"] > 0
    assert all(gui.fleet.sessions[i]._live == 0 for i in ids)  # the preflight's references were given back
    assert gui.post("/api/preflight", {"device_ids": ["0123"]}).status_code == 404


def test_a_transient_identify_lock_does_not_fail_the_batch(rig):
    """M3 item 3, the route side: "identify" is not a lock for the checks, and the job waits for it."""
    gui = rig(1)
    (device_id,) = _gather(gui, 1)
    session = gui.fleet.sessions[device_id]

    async def hold_identify() -> None:
        async with session.operation("identify"):
            await asyncio.sleep(0.3)

    with gui.ws() as ws:
        ws.receive_json()
        gui.client.portal.start_task_soon(hold_identify)
        _wait(lambda: session.busy == "identify")
        created = gui.post("/api/batches", {"device_ids": [device_id]})
        assert created.status_code == 202
        final = _until(ws, _batch(lambda d: d["state"] == "done"))[-1]["data"]
        assert final["jobs"][0]["state"] == "succeeded" and final["jobs"][0]["error"] is None


def test_a_batch_fired_while_a_regather_identifies_the_sensor_succeeds(rig):
    """The gather stop on fire (G20) lets an in-flight re-gather finish: cancelling it would
    close the link the waiting job is about to calibrate."""
    gui = rig(1)
    (device_id,) = _gather(gui, 1)
    session = gui.fleet.sessions[device_id]
    dev = gui.sim.devices[0]
    gui.post("/api/sim/drop/1")
    _wait(lambda: session.state is LinkState.LOST)
    dev.response_delay = 10.0  # 0.1 s of wall time: the identify read holds its lock that long
    gui.post("/api/gather/start")
    with gui.ws() as ws:
        ws.receive_json()
        gui.post("/api/sim/press/1")
        _wait(lambda: session.busy == "identify")
        assert gui.post("/api/batches", {"device_ids": [device_id]}).status_code == 202
        final = _until(ws, _batch(lambda d: d["state"] == "done"))[-1]["data"]
        assert final["jobs"][0]["state"] == "succeeded", final["jobs"][0]
    assert not gui.fleet.gathering
