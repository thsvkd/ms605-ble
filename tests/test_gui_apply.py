"""ms605 gui settings editing: config read, server-side preview with diff rows and
risks, the one apply job (apply / rollback / clone) with its polled verify and
per-sensor results, config_rev and `stale`, the job locks against batches and
release, and request validation (docs/GUI_API.md 15.5-15.7, 15.10.1). Every value
is synthetic."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from conftest import recv_until

from ms605.calibration import CalibrationJob
from ms605.events import ApplyResult, ApplyStatus, BatchState, LinkState
from ms605.models import encode_zone_thresholds
from ms605.protocol import (
    SENSITIVITY_PRESETS,
    TAG_DETECT_MODE,
    TAG_READ_REQUEST,
    TAG_SENSITIVITY,
    TAG_SYSTEM_SUBCOMMAND,
    TAG_ZONE_THRESHOLDS,
    Sensitivity,
)

pytestmark = pytest.mark.timeout(60)

SIM1, SIM2, SIM3 = (f"53494d36303500{i:02x}" for i in (1, 2, 3))
MEDIUM = list(zip(*SENSITIVITY_PRESETS[Sensitivity.MEDIUM], strict=True))
NONE7 = [None] * 7
RISK_ORDER = [
    "absolute_overwrite", "large_change", "beyond_ui_range", "zone_off", "subsensor_off",
    "subsensor_no_zone", "sensitivity_only", "dnd_on", "learning_skipped",
]  # fmt: skip
SNAPSHOT_NAME = "20260101T000000000000Z"


# -- helpers (test_gui_advanced.py uses them too) ------------------------------------------


def _id(index: int) -> str:
    return f"53494d36303500{index:02x}"


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def _gather(gui, count: int, *, lag: bool = False) -> list[str]:
    """Connect the first `count` simulated sensors. Without `lag` a tag51 write reads back at once."""
    if not lag:
        for dev in gui.sim.devices:
            dev.apply_delay = None
    gui.post("/api/gather/start")
    gui.post("/api/sim/press-all")
    ids = [_id(i) for i in range(1, count + 1)]
    _wait(lambda: all(i in gui.fleet.sessions and gui.fleet.sessions[i].state is LinkState.CONNECTED for i in ids))
    gui.post("/api/gather/stop")
    return ids


def _hub(gui):
    return gui.app.state.hub


def _code(response) -> str:
    return response.json()["error"]["code"]


def _thresholds(mode: str, trigger=NONE7, maintain=NONE7) -> dict:
    return {"mode": mode, "trigger": list(trigger), "maintain": list(maintain)}


def _draft(targets, expect_rev=None, **changes) -> dict:
    body = {"targets": list(targets), "changes": changes}
    if expect_rev is not None:
        body["expect_rev"] = expect_rev
    return body


def _write(gui, path: str, body: dict):
    """POST a write route with every sensor's current config_rev (what a fresh preview gives), unless `body` has one."""
    ids = [*body.get("targets", []), *(i["device_id"] for i in body.get("items", []))]
    if "source" in body:
        ids.append(body["source"])
    return gui.post(path, {"expect_rev": {i: _hub(gui).applies.rev(i) for i in ids}, **body})


def _done(gui, response) -> dict:
    """Wait for the job a 202 response started; return its final view."""
    assert response.status_code == 202, response.text
    apply_id = response.json()["apply_id"]
    _wait(lambda: _hub(gui).applies.current.apply_id == apply_id and not _hub(gui).applies.active())
    final = gui.get(f"/api/apply/{apply_id}")
    assert final.status_code == 200
    return final.json()


def _items(view: dict) -> dict[str, dict]:
    return {i["device_id"]: i for i in view["items"]}


def _writes_after(dev, start: int) -> list[int]:
    """Tags written by the frames the device received from index `start` on."""
    skip = (TAG_READ_REQUEST, TAG_SYSTEM_SUBCOMMAND)
    return [t for f in dev.frames_in[start:] for t, _ in f.attributes if t not in skip]


def _frame_indices(dev, *, write: int | None = None, read: int | None = None) -> list[int]:
    out = []
    for n, f in enumerate(dev.frames_in):
        for t, v in f.attributes:
            reads = read is not None and t == TAG_READ_REQUEST and v == bytes([read])
            if reads or (write is not None and t == write):
                out.append(n)
                break
    return out


def _until(ws, predicate) -> list[dict]:
    return recv_until(ws, predicate, limit=5000)


def _apply_msg(apply_id: str, state: str | None = None):
    return lambda m: m["type"] == "apply" and m["data"]["apply_id"] == apply_id and state in (None, m["data"]["state"])


def _item_states(messages: list[dict], device_id: str) -> list[str]:
    seen: list[str] = []
    for m in messages:
        if m["type"] == "apply":
            state = _items(m["data"])[device_id]["state"]
            if not seen or seen[-1] != state:
                seen.append(state)
    return seen


def _is_subsequence(part: list[str], whole: list[str]) -> bool:
    it = iter(whole)
    return all(x in it for x in part)


def _risks_ordered(preview: dict) -> None:
    for item in preview["items"]:
        for row in item["changes"]:
            assert row["risks"] == [r for r in RISK_ORDER if r in row["risks"]], row
        assert item["risks"] == [r for r in RISK_ORDER if r in item["risks"]], item
    assert preview["risks"] == [r for r in RISK_ORDER if r in preview["risks"]]


# -- config read -----------------------------------------------------------------------------


def test_read_config(make_gui):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    got = gui.get(f"/api/sensors/{SIM1}/config")
    assert got.status_code == 200
    view = got.json()
    profile = view["profile"]
    assert profile["sensitivity"] == 2 and profile["zone_thresholds"][0] == {"trigger": 95, "maintain": 40}
    assert view["distances_m"] == [0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6]
    assert profile["dnd"] is False and profile["subsensor_zones"] == [[0, 1, 2], [3, 4], [5, 6]]
    assert profile["subsensor_timing"] == [[5, 30]] * 3 and view["config_rev"] == 0
    assert profile["zone_enable"] == [True] * 7 and profile["subsensor_enable"] == [True] * 3

    gui.post("/api/sim/drop/2")
    _wait(lambda: gui.fleet.sessions[SIM2].state is LinkState.LOST)
    lost = gui.get(f"/api/sensors/{SIM2}/config")
    assert lost.status_code == 409 and _code(lost) == "not_connected"
    assert gui.get("/api/sensors/0123/config").status_code == 404


# -- preview -> apply -> polled verify -----------------------------------------------------------


def test_preview_then_apply_is_polled_until_verified(make_gui):
    gui = make_gui(sim_count=1)
    (device_id,) = _gather(gui, 1, lag=True)
    dev = gui.sim.devices[0]
    dev.apply_delay = 100.0  # 1 s of wall time: the first read-back still shows the old value
    edit = {"zone_thresholds": _thresholds("absolute", [100, *NONE7[1:]])}

    start = len(dev.frames_in)
    preview = gui.post("/api/drafts/preview", _draft([device_id], **edit))
    assert preview.status_code == 200
    body = preview.json()
    assert body["kind"] == "apply" and body["risks"] == [] and len(body["items"]) == 1
    item = body["items"][0]
    assert item["error"] is None and item["config_rev"] == 0
    assert item["changes"] == [
        {"section": "zone_thresholds", "index": 0, "part": "trigger", "before": 95, "after": 100, "risks": []}
    ]
    assert item["before"]["zone_thresholds"][0] == {"trigger": 95, "maintain": 40}
    assert item["after"]["zone_thresholds"][0] == {"trigger": 100, "maintain": 40}
    assert item["after"]["zone_thresholds"][1:] == item["before"]["zone_thresholds"][1:]
    assert _writes_after(dev, start) == []  # a preview only reads

    with gui.ws() as ws:
        ws.receive_json()
        created = _write(gui, "/api/apply", _draft([device_id], expect_rev={device_id: 0}, **edit))
        assert created.status_code == 202
        view = created.json()
        assert view["kind"] == "apply" and view["state"] == "running" and view["source"] is None
        assert view["sections"] == ["zone_thresholds"] and [i["state"] for i in view["items"]] == ["queued"]
        got = _until(ws, _apply_msg(view["apply_id"], "done"))
    assert _is_subsequence(["queued", "applying", "verified"], _item_states(got, device_id))
    final = got[-1]["data"]["items"][0]
    assert final["applied"] == ["zone_thresholds"] and final["mismatched"] == [] and final["snapshot"]
    assert final["finished_at"] is not None and final["error"] is None
    write = _frame_indices(dev, write=TAG_ZONE_THRESHOLDS)
    assert len(write) == 1
    assert len([n for n in _frame_indices(dev, read=TAG_ZONE_THRESHOLDS) if n > write[0]]) >= 2  # polled
    assert dev.thresholds[0] == (100, 40)
    sensors = [m["data"] for m in got if m["type"] == "sensor" and m["data"]["device_id"] == device_id]
    assert sensors and sensors[-1]["config_rev"] == 1
    assert sensors[-1]["last_snapshot"]["name"] == final["snapshot"]
    snapshots = gui.get(f"/api/sensors/{device_id}/snapshots").json()["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["reason"] == "apply" and snapshots[0]["sections"] == ["zone_thresholds"]


def test_a_write_still_unseen_after_the_verify_window_is_partial(make_gui):
    gui = make_gui(sim_count=1)
    (device_id,) = _gather(gui, 1, lag=True)
    gui.sim.devices[0].apply_delay = 1000.0  # 10 s of wall time > the 3 s verify
    edit = {"zone_thresholds": _thresholds("absolute", [100, *NONE7[1:]])}
    final = _items(_done(gui, _write(gui, "/api/apply", _draft([device_id], **edit))))[device_id]
    assert final["state"] == "partial" and final["mismatched"] == ["zone_thresholds"]


# -- partial failure and rollback (the M4 done criterion) ----------------------------------------


def test_partial_failure_then_rollback_restores(make_gui):
    gui = make_gui(sim_count=3)
    ids = _gather(gui, 3)
    d1, d2, d3 = gui.sim.devices
    d2.inject_status(5)  # the device refuses its next write
    edit = {"sensitivity": 3, "zone_thresholds": _thresholds("relative", [5] * 7)}
    with gui.ws() as ws:
        ws.receive_json()
        created = _write(gui, "/api/apply", _draft(ids, **edit))
        assert created.status_code == 202
        last = _until(ws, _apply_msg(created.json()["apply_id"], "done"))[-1]["data"]
    items = _items(last)
    assert items[SIM1]["state"] == items[SIM3]["state"] == "verified"
    failed = items[SIM2]
    assert failed["state"] == "failed" and failed["snapshot"] and "status 5" in failed["error"]
    assert failed["applied"] == []
    counts = {s: sum(1 for i in last["items"] if i["state"] == s) for s in ("verified", "failed")}
    assert counts == {"verified": 2, "failed": 1}
    assert gui.get(f"/api/apply/{last['apply_id']}").json() == last
    plus5 = [(t + 5, m) for t, m in MEDIUM]
    assert d1.thresholds == d3.thresholds == plus5 and d1.sensitivity == d3.sensitivity == 3
    assert d2.sensitivity == 2 and d2.thresholds == MEDIUM

    rollback = {
        "items": [
            {"device_id": SIM1, "snapshot": items[SIM1]["snapshot"]},
            {"device_id": SIM2, "snapshot": failed["snapshot"]},
        ]
    }
    undone = _done(gui, _write(gui, "/api/rollback", rollback))
    assert undone["kind"] == "rollback" and undone["sections"] == ["sensitivity", "zone_thresholds"]
    assert [(i["state"], i["restore"]) for i in undone["items"]] == [
        ("verified", items[SIM1]["snapshot"]),
        ("verified", failed["snapshot"]),
    ]
    assert d1.sensitivity == d2.sensitivity == 2 and d1.thresholds == d2.thresholds == MEDIUM
    snapshots = gui.get(f"/api/sensors/{SIM1}/snapshots").json()["snapshots"]
    assert [s["reason"] for s in snapshots] == ["rollback", "apply"]

    redo = _done(gui, _write(gui, "/api/rollback", {"items": [{"device_id": SIM1, "snapshot": snapshots[0]["name"]}]}))
    assert redo["items"][0]["state"] == "verified"  # a rollback can itself be undone
    assert d1.sensitivity == 3 and d1.thresholds == plus5

    missing = _write(gui, "/api/rollback", {"items": [{"device_id": SIM1, "snapshot": SNAPSHOT_NAME}]})
    assert missing.status_code == 404
    bad = gui.post("/api/rollback/preview", {"items": [{"device_id": SIM1, "snapshot": "nope"}]})
    assert bad.status_code == 422 and _code(bad) == "invalid_request"


def test_relative_thresholds_resolve_per_sensor(make_gui):
    gui = make_gui(sim_count=3)
    calibrated = {
        0: [(60 + z, 30) for z in range(7)],
        1: [(70 + z, 32) for z in range(6)] + [(3, 2)],
        2: [(55 + z, 28) for z in range(7)],
    }
    for index, pairs in calibrated.items():
        gui.sim.devices[index].tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(pairs)
    ids = _gather(gui, 3)
    maintain = [None, None, -3, None, None, None, None]
    edit = {"zone_thresholds": _thresholds("relative", [5] * 7, maintain)}
    preview = gui.post("/api/drafts/preview", _draft(ids, **edit)).json()
    assert preview["risks"] == []
    def moved(index: int) -> list[tuple[int, int]]:
        return [(t + 5, m - 3 if z == 2 else m) for z, (t, m) in enumerate(calibrated[index])]

    for index, item in enumerate(preview["items"]):
        expected = [{"trigger": t, "maintain": m} for t, m in moved(index)]
        assert item["error"] is None and item["after"]["zone_thresholds"] == expected
        assert len(item["changes"]) == 8 and item["risks"] == []
    final = _done(gui, _write(gui, "/api/apply", _draft(ids, **edit)))
    assert [i["state"] for i in final["items"]] == ["verified"] * 3
    for index, dev in enumerate(gui.sim.devices):
        assert dev.thresholds == moved(index)

    # sensor 2's zone 6 trigger is now 8: -10 would be -2
    edit = {"zone_thresholds": _thresholds("relative", [*NONE7[:6], -10])}
    preview = gui.post("/api/drafts/preview", _draft(ids, **edit)).json()
    errors = {i["device_id"]: i["error"] for i in preview["items"]}
    assert errors[SIM1] is None and errors[SIM3] is None and "outside 0..65535" in errors[SIM2]
    broken = _items(preview)[SIM2]
    assert broken["before"] is None and broken["after"] is None and broken["changes"] == []
    start = len(gui.sim.devices[1].frames_in)
    final = _items(_done(gui, _write(gui, "/api/apply", _draft(ids, **edit))))
    assert final[SIM2]["state"] == "failed" and final[SIM2]["snapshot"] is None
    assert final[SIM1]["state"] == final[SIM3]["state"] == "verified"
    assert TAG_ZONE_THRESHOLDS not in _writes_after(gui.sim.devices[1], start)


# -- risks ----------------------------------------------------------------------------------------


def test_risk_flags(make_gui):
    gui = make_gui(sim_count=3)
    ids = _gather(gui, 3)
    d1, _, d3 = gui.sim.devices

    def preview(targets, **edit) -> dict:
        got = gui.post("/api/drafts/preview", _draft(targets, **edit))
        assert got.status_code == 200, got.text
        _risks_ordered(got.json())
        return got.json()

    absolute = {"zone_thresholds": _thresholds("absolute", [60, *NONE7[1:]])}
    two = preview(ids[:2], **absolute)
    assert two["risks"] == ["absolute_overwrite", "large_change"]
    for item in two["items"]:
        assert item["changes"][0]["risks"] == ["absolute_overwrite", "large_change"]  # 95 -> 60
    assert preview([SIM1], **absolute)["risks"] == ["large_change"]  # one sensor: absolute is the norm (G32)
    assert preview(ids[:2], zone_thresholds=_thresholds("relative", [5] * 7))["risks"] == []

    custom = [(80 + z, 20) for z in range(7)]
    d1.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(custom)
    clone = gui.post("/api/clone/preview", {"source": SIM1, "targets": [SIM2], "sections": ["zone_thresholds"]})
    assert clone.status_code == 200 and clone.json()["kind"] == "clone"
    assert "absolute_overwrite" in clone.json()["risks"]

    applied = _done(gui, _write(gui, "/api/apply", _draft([SIM3], **absolute)))
    back = {"items": [{"device_id": SIM3, "snapshot": applied["items"][0]["snapshot"]}]}
    undo = gui.post("/api/rollback/preview", back).json()
    assert undo["kind"] == "rollback" and undo["items"][0]["changes"][0]["after"] == 95
    assert "absolute_overwrite" not in undo["risks"]

    d3.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds([(495, 40)] * 7)
    beyond = preview([SIM3], zone_thresholds=_thresholds("relative", [10, *NONE7[1:]]))
    assert beyond["items"][0]["changes"][0]["risks"] == ["beyond_ui_range"]
    # a calibrated value above the app's axis can be lowered a little: absolute is a U16, not a UI value
    d3.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds([(620, 40)] * 7)
    lower = preview([SIM3], zone_thresholds=_thresholds("absolute", [619, *NONE7[1:]]))
    assert [(r["before"], r["after"], r["risks"]) for r in lower["items"][0]["changes"]] == [
        (620, 619, ["beyond_ui_range"])
    ]
    off = preview([SIM2], zone_enable=[False] + [True] * 6)
    assert off["items"][0]["changes"] == [
        {"section": "zone_enable", "index": 0, "part": "value", "before": True, "after": False, "risks": ["zone_off"]}
    ]
    assert preview([SIM2], sensitivity=3)["items"][0]["changes"][0]["risks"] == ["sensitivity_only"]
    both = preview([SIM2], sensitivity=3, zone_thresholds=_thresholds("relative", [5] * 7))
    assert "sensitivity_only" not in both["risks"]


# -- locks between jobs (G27) ---------------------------------------------------------------------


def test_a_calibrating_sensor_refuses_every_edit(make_gui, monkeypatch):
    monkeypatch.setitem(CalibrationJob.__init__.__kwdefaults__, "progress_interval", 0.1)
    gui = make_gui(sim_count=3)
    _gather(gui, 3)
    assert gui.post("/api/batches", {"device_ids": [SIM1]}).status_code == 202
    _wait(lambda: _hub(gui).batches.current.state is BatchState.RUNNING)
    edit = {"sensitivity": 3}
    refused = [
        gui.get(f"/api/sensors/{SIM1}/config"),
        gui.post("/api/drafts/preview", _draft([SIM1], **edit)),
        _write(gui, "/api/apply", _draft([SIM1], **edit)),
        gui.post("/api/rollback/preview", {"items": [{"device_id": SIM1, "snapshot": SNAPSHOT_NAME}]}),
        _write(gui, "/api/rollback", {"items": [{"device_id": SIM1, "snapshot": SNAPSHOT_NAME}]}),
        gui.post("/api/clone/preview", {"source": SIM2, "targets": [SIM1], "sections": ["sensitivity"]}),
        _write(gui, "/api/clone", {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"]}),
        gui.post("/api/time-sync", {"device_ids": [SIM1]}),
        gui.get(f"/api/sensors/{SIM1}/device-history"),
    ]
    assert [(r.status_code, _code(r)) for r in refused] == [(409, "batch_active")] * len(refused)
    assert gui.post("/api/drafts/preview", _draft([SIM2], **edit)).status_code == 200


def test_a_running_apply_job_holds_its_sensors(make_gui):
    gui = make_gui(sim_count=3)
    _gather(gui, 3)
    d1 = gui.sim.devices[0]
    d1.response_delay = 50.0  # 0.5 s of wall time per response: SIM1 applies for a while, SIM2 waits
    edit = {"zone_thresholds": _thresholds("absolute", [100, *NONE7[1:]])}
    created = _write(gui, "/api/apply", _draft([SIM1, SIM2], **edit))
    assert created.status_code == 202

    second = _write(gui, "/api/apply", _draft([SIM3], **edit))
    assert second.status_code == 409 and _code(second) == "apply_active"  # one job in the whole server (G25)
    batch = gui.post("/api/batches", {"device_ids": [SIM2]})  # queued in the job: not locked, but held
    assert batch.status_code == 409 and _code(batch) == "apply_active"
    for body in ({"device_ids": [SIM2]}, {}):
        released = gui.post("/api/release", body)
        assert released.status_code == 409 and _code(released) == "apply_active"
    assert gui.fleet.sessions[SIM2].state is LinkState.CONNECTED
    busy = gui.post("/api/drafts/preview", _draft([SIM1], **edit))
    assert busy.status_code == 409 and _code(busy) == "apply_active"

    d1.response_delay = 0.0
    assert [i["state"] for i in _done(gui, created)["items"]] == ["verified", "verified"]
    assert gui.post("/api/drafts/preview", _draft([SIM1], **edit)).status_code == 200
    assert gui.post("/api/release", {"device_ids": [SIM3]}).status_code == 204


def test_a_change_after_the_preview_makes_the_apply_stale(make_gui):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    for dev in gui.sim.devices:
        dev.calibration_secs = 60
    edit = {"sensitivity": 3}
    rev = gui.post("/api/drafts/preview", _draft([SIM1], **edit)).json()["items"][0]["config_rev"]
    assert rev == 0
    _done(gui, _write(gui, "/api/apply", _draft([SIM1], sensitivity=1)))  # another screen changes SIM1
    start = len(gui.sim.devices[0].frames_in)
    stale = _write(gui, "/api/apply", _draft([SIM1], expect_rev={SIM1: rev}, **edit))
    assert stale.status_code == 409 and _code(stale) == "stale"
    assert _writes_after(gui.sim.devices[0], start) == []
    assert gui.get(f"/api/sensors/{SIM1}/config").json()["config_rev"] == 1

    assert gui.post("/api/batches", {"device_ids": [SIM2]}).status_code == 202  # a calibration counts too
    _wait(lambda: not _hub(gui).batches.active())
    sensors = {s["device_id"]: s for s in gui.get("/api/state").json()["sensors"]}
    assert _hub(gui).batches.view().jobs[0].state == "succeeded"
    assert sensors[SIM2]["config_rev"] == 1


# -- clone -------------------------------------------------------------------------------------


def test_clone_copies_the_source_read_now(make_gui):
    gui = make_gui(sim_count=3)
    ids = _gather(gui, 3)
    src = gui.sim.devices[0]
    custom = [(80 + z, 20 + z) for z in range(7)]
    src.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(custom)
    src.tags[TAG_SENSITIVITY] = bytes([Sensitivity.CUSTOM])
    body = {"source": SIM1, "targets": [SIM2, SIM3], "sections": ["zone_thresholds", "sensitivity"]}
    preview = gui.post("/api/clone/preview", body)
    assert preview.status_code == 200 and preview.json()["source_rev"] == 0
    assert preview.json()["kind"] == "clone" and [i["device_id"] for i in preview.json()["items"]] == [SIM2, SIM3]
    assert preview.json()["items"][0]["after"]["zone_thresholds"][0] == {"trigger": 80, "maintain": 20}
    final = _done(gui, _write(gui, "/api/clone", body))
    assert final["kind"] == "clone" and final["source"] == SIM1
    assert final["sections"] == ["sensitivity", "zone_thresholds"]
    assert [i["state"] for i in final["items"]] == ["verified", "verified"]
    for dev in gui.sim.devices[1:]:
        assert dev.tags[TAG_ZONE_THRESHOLDS] == src.tags[TAG_ZONE_THRESHOLDS]
        assert dev.tags[TAG_SENSITIVITY] == src.tags[TAG_SENSITIVITY]

    src.tags[TAG_DETECT_MODE] = b"\x04"  # the source is learning: the core never clones that (it would start one)
    learning = {"source": SIM1, "targets": [SIM2], "sections": ["detect_mode"]}
    assert gui.post("/api/clone/preview", learning).json()["risks"] == ["learning_skipped"]
    item = _done(gui, _write(gui, "/api/clone", learning))["items"][0]
    assert item["skipped"] == ["detect_mode"] and item["applied"] == [] and item["state"] == "verified"

    # the source moved after the preview: the preview's source_rev makes the clone stale
    seen = gui.post("/api/clone/preview", {**body, "targets": [SIM3]}).json()
    assert seen["source_rev"] == _hub(gui).applies.rev(SIM1)
    _done(gui, _write(gui, "/api/apply", _draft([SIM1], sensitivity=1)))
    expect = {SIM1: seen["source_rev"], SIM3: seen["items"][0]["config_rev"]}
    start = len(gui.sim.devices[2].frames_in)
    stale = _write(gui, "/api/clone", {**body, "targets": [SIM3], "expect_rev": expect})
    assert stale.status_code == 409 and _code(stale) == "stale"
    assert _writes_after(gui.sim.devices[2], start) == []
    assert gui.post("/api/drafts/preview", _draft([SIM1], sensitivity=2)).json()["source_rev"] is None

    inside = _write(gui, "/api/clone", {"source": SIM1, "targets": [SIM1, SIM2], "sections": ["sensitivity"]})
    assert inside.status_code == 422 and _code(inside) == "invalid_request"
    gui.post("/api/sim/drop/1")
    _wait(lambda: gui.fleet.sessions[ids[0]].state is LinkState.LOST)
    lost = gui.post("/api/clone/preview", {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"]})
    assert lost.status_code == 409 and _code(lost) == "not_connected"


def _gate_source_read(gui, monkeypatch, device_id: str) -> threading.Event:
    """Hold that sensor's next config reads until the returned event is set (a slow source)."""
    gate = threading.Event()
    ms = gui.fleet.sessions[device_id].ms
    real = ms.read_config

    async def held(*args, **kw):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return await real(*args, **kw)

    monkeypatch.setattr(ms, "read_config", held)
    return gate


def _clone_in_thread(gui, body: dict) -> tuple[threading.Thread, list]:
    result: list = []
    thread = threading.Thread(target=lambda: result.append(gui.post("/api/clone", body)))
    thread.start()
    _wait(lambda: gui.fleet.sessions[body["source"]].busy == "read")
    return thread, result


def test_a_slow_clone_source_read_holds_no_other_job(make_gui, monkeypatch):
    gui = make_gui(sim_count=3)
    _gather(gui, 3)
    gate = _gate_source_read(gui, monkeypatch, SIM1)
    opener = threading.Timer(3.0, gate.set)  # the read ends on its own: a held lock would only delay the release
    opener.start()
    body = {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"], "expect_rev": {SIM1: 0, SIM2: 0}}
    cloning, result = _clone_in_thread(gui, body)
    try:
        released = gui.post("/api/release", {"device_ids": [SIM3]})  # takes `operation`, as apply and batches do
        assert released.status_code == 204
        assert not gate.is_set()  # answered while the source was still being read
    finally:
        gate.set()
        opener.cancel()
        cloning.join(timeout=20)
    assert [i["state"] for i in _done(gui, result[0])["items"]] == ["verified"]


def test_a_source_change_during_the_clone_read_is_stale(make_gui, monkeypatch):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    gate = _gate_source_read(gui, monkeypatch, SIM1)
    body = {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"], "expect_rev": {SIM1: 0, SIM2: 0}}
    start = len(gui.sim.devices[1].frames_in)
    cloning, result = _clone_in_thread(gui, body)
    try:
        written = ApplyResult(
            address=gui.fleet.sessions[SIM1].address, device_id=SIM1, reason="apply", status=ApplyStatus.OK,
            applied=("sensitivity",), skipped=(), mismatched=(), snapshot=SNAPSHOT_NAME, error=None,
        )  # fmt: skip
        gui.client.portal.call(gui.fleet.bus.emit, written)  # another write lands on the source mid-read
        assert _hub(gui).applies.rev(SIM1) == 1
    finally:
        gate.set()
        cloning.join(timeout=20)
    assert result[0].status_code == 409 and _code(result[0]) == "stale"
    assert SIM1 in result[0].json()["error"]["message"]
    assert not _hub(gui).applies.active() and _writes_after(gui.sim.devices[1], start) == []


# -- every screen sees the same job ------------------------------------------------------------


def test_every_client_sees_the_same_job_and_a_late_joiner_catches_up(make_gui):
    gui = make_gui(sim_count=2)
    ids = _gather(gui, 2)
    gui.sim.devices[0].response_delay = 10.0  # 0.1 s of wall time per response
    edit = {"zone_thresholds": _thresholds("absolute", [100, *NONE7[1:]])}
    with gui.ws() as ws_a, gui.ws() as ws_b:
        ws_a.receive_json()
        ws_b.receive_json()
        created = _write(gui, "/api/apply", _draft(ids, **edit))
        assert created.status_code == 202
        apply_id = created.json()["apply_id"]
        got_a = _until(ws_a, lambda m: m["type"] == "apply" and _items(m["data"])[SIM1]["state"] == "applying")
        with gui.ws() as ws_c:
            snapshot = ws_c.receive_json()
            joined = snapshot["data"]["apply"]
            assert joined["apply_id"] == apply_id and joined["state"] == "running"
            got_c = _until(ws_c, _apply_msg(apply_id, "done"))
        got_a += _until(ws_a, _apply_msg(apply_id, "done"))
        got_b = _until(ws_b, _apply_msg(apply_id, "done"))
    ordered_a = [(m["seq"], m["type"], m["data"]) for m in got_a if m["seq"] is not None]
    ordered_b = [(m["seq"], m["type"], m["data"]) for m in got_b if m["seq"] is not None]
    assert ordered_a == ordered_b
    before_join = [m for m in ordered_a if m[0] <= snapshot["seq"] and m[1] == "apply"]
    assert before_join[-1][2] == joined  # the late joiner's snapshot holds what A saw last
    assert [m for m in ordered_a if m[0] > snapshot["seq"]] == [
        (m["seq"], m["type"], m["data"]) for m in got_c if m["seq"] is not None
    ]
    assert gui.get(f"/api/apply/{apply_id}").json() == got_a[-1]["data"]
    assert gui.get(f"/api/apply/{'0' * 32}").status_code == 404
    assert gui.get("/api/state").json()["apply"] == got_a[-1]["data"]


# -- validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _draft([SIM1, SIM1], sensitivity=3),
        _draft([SIM1]),
        _draft([SIM1], zone_thresholds=_thresholds("absolute", [-1, *NONE7[1:]])),
        _draft([SIM1], zone_thresholds=_thresholds("relative", [501, *NONE7[1:]])),
        _draft([SIM1], zone_thresholds=_thresholds("absolute", [65536, *NONE7[1:]])),
        _draft([SIM1], zone_thresholds=_thresholds("relative")),
        _draft([SIM1], sensitivity=True),
        _draft([SIM1], dnd=1),
        _draft([SIM1], zone_thresholds=_thresholds("absolute", [5.0, *NONE7[1:]])),
        _draft([SIM1], zone_enable=[1] * 7),
        _draft([SIM1], foo=1),
        {"targets": [SIM1], "changes": {"sensitivity": 3}, "extra": True},
    ],
    ids=[
        "duplicate-targets", "empty", "absolute-negative", "501", "absolute-over-u16", "no-zone", "bool-sensitivity",
        "int-dnd", "float-threshold", "int-flags", "unknown-field", "unknown-top-field",
    ],
)  # fmt: skip
def test_invalid_drafts_are_refused(gui, body):
    for path in ("/api/drafts/preview", "/api/apply"):
        got = gui.post(path, {"expect_rev": {SIM1: 0}, **body})  # refused for its own fault, not a missing rev
        assert got.status_code == 422 and _code(got) == "invalid_request", (path, got.text)


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/apply", _draft([SIM1, SIM2], sensitivity=3)),
        ("/api/apply", {**_draft([SIM1, SIM2], sensitivity=3), "expect_rev": None}),
        ("/api/apply", _draft([SIM1, SIM2], expect_rev={SIM1: 0}, sensitivity=3)),
        ("/api/rollback", {"items": [{"device_id": SIM1, "snapshot": SNAPSHOT_NAME}]}),
        ("/api/rollback", {"items": [{"device_id": SIM1, "snapshot": SNAPSHOT_NAME}], "expect_rev": {SIM2: 0}}),
        ("/api/clone", {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"]}),
        ("/api/clone", {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"], "expect_rev": {SIM2: 0}}),
        ("/api/clone", {"source": SIM1, "targets": [SIM2], "sections": ["sensitivity"], "expect_rev": {SIM1: 0}}),
    ],
    ids=[
        "apply-missing", "apply-null", "apply-one-target", "rollback-missing", "rollback-other-sensor",
        "clone-missing", "clone-no-source", "clone-no-target",
    ],
)  # fmt: skip
def test_a_write_needs_expect_rev_for_every_sensor(make_gui, path, body):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    starts = [len(dev.frames_in) for dev in gui.sim.devices]
    got = gui.post(path, body)
    assert got.status_code == 422 and _code(got) == "invalid_request", got.text
    assert _hub(gui).applies.current is None
    for dev, start in zip(gui.sim.devices, starts, strict=True):
        assert _writes_after(dev, start) == []
