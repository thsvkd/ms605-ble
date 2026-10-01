"""ms605 gui advanced settings and history: DND and sub-sensor sections through the
same draft/apply/rollback flow (G33), time sync as an action (G34), calibration
history, settings backups and the EXPERIMENTAL device history (G35)
(docs/GUI_API.md 15.5.8-15.5.11, 15.10.1). Every value is synthetic."""

from __future__ import annotations

import struct
import threading

import pytest
from test_gui_apply import _code, _done, _draft, _gather, _hub, _wait, _write

from ms605.calibration import CalibrationJob
from ms605.driver import MS605
from ms605.events import BusyChanged, LinkState
from ms605.gui.apply import DND_READ_TIMEOUT_S
from ms605.models import ConfigProfile, encode_presence_absence_times, encode_segment_map
from ms605.protocol import (
    TAG_DND,
    TAG_LIGHT_HISTORY_COUNT,
    TAG_LIGHT_HISTORY_PUSH,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_PRESENCE_HISTORY_COUNT,
    TAG_PRESENCE_HISTORY_PUSH,
    TAG_SEGMENT_MAP,
    TAG_TIME_SYNC,
    WRITE_TIMEOUT_S,
)

pytestmark = pytest.mark.timeout(60)

SIM1, SIM2, SIM3 = (f"53494d36303500{i:02x}" for i in (1, 2, 3))
ADDR1 = "02:00:00:00:00:01"


def test_dnd_and_sub_sensors_take_the_draft_flow(make_gui):
    gui = make_gui(sim_count=1)
    (device_id,) = _gather(gui, 1)
    dev = gui.sim.devices[0]
    zones_before, timing_before = dev.tags[TAG_SEGMENT_MAP], dev.tags[TAG_PRESENCE_ABSENCE_TIMES]
    edit = {
        "dnd": True,
        "subsensor_timing": [[10, 60], [5, 30], [5, 30]],
        "subsensor_zones": [[0], [3, 4], [5, 6]],
    }
    preview = gui.post("/api/drafts/preview", _draft([device_id], **edit)).json()
    item = preview["items"][0]
    assert preview["risks"] == ["dnd_on"] and item["before"]["dnd"] is False and item["after"]["dnd"] is True
    assert [(r["section"], r["index"], r["part"]) for r in item["changes"]] == [
        ("subsensor_zones", 0, "value"),
        ("subsensor_timing", 0, "presence_s"),
        ("subsensor_timing", 0, "absence_s"),
        ("dnd", None, "value"),
    ]
    final = _done(gui, _write(gui, "/api/apply", _draft([device_id], **edit)))
    assert final["sections"] == ["subsensor_zones", "subsensor_timing", "dnd"]
    result = final["items"][0]
    assert result["state"] == "verified" and result["applied"][-1] == "dnd"
    assert dev.tags[TAG_DND] == b"\x01"
    assert dev.tags[TAG_SEGMENT_MAP] == encode_segment_map([[0], [3, 4], [5, 6]])
    assert dev.tags[TAG_PRESENCE_ABSENCE_TIMES] == encode_presence_absence_times([(10, 60), (5, 30), (5, 30)])

    back = {"items": [{"device_id": device_id, "snapshot": result["snapshot"]}]}
    assert _done(gui, _write(gui, "/api/rollback", back))["items"][0]["state"] == "verified"
    assert dev.tags[TAG_DND] == b"\x00"
    assert dev.tags[TAG_SEGMENT_MAP] == zones_before and dev.tags[TAG_PRESENCE_ABSENCE_TIMES] == timing_before

    empty = gui.post("/api/drafts/preview", _draft([device_id], subsensor_zones=[[], [3, 4], [5, 6]])).json()
    assert empty["items"][0]["changes"][0]["risks"] == ["subsensor_no_zone"]
    off = gui.post("/api/drafts/preview", _draft([device_id], subsensor_enable=[False, True, True])).json()
    assert off["risks"] == ["subsensor_off"]


def test_editor_dnd_reads_wait_at_most_the_short_deadline(make_gui, monkeypatch):
    timeouts: list[float] = []
    real = MS605.read_dnd

    async def spy(self, *, timeout: float = WRITE_TIMEOUT_S) -> bool:
        timeouts.append(timeout)
        return await real(self, timeout=timeout)

    monkeypatch.setattr(MS605, "read_dnd", spy)
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    assert gui.post("/api/drafts/preview", _draft([SIM1], dnd=True)).status_code == 200
    body = {"source": SIM1, "targets": [SIM2], "sections": ["dnd"]}
    assert gui.post("/api/clone/preview", body).status_code == 200  # the source, then the target's preview
    assert timeouts == [DND_READ_TIMEOUT_S] * 3
    timeouts.clear()
    created = _write(gui, "/api/clone", body)
    assert created.status_code == 202
    assert timeouts[0] == DND_READ_TIMEOUT_S  # the clone's source read (the job's own reads come after it)
    _done(gui, created)


def test_time_sync_writes_each_sensor_clock(make_gui, monkeypatch):
    monkeypatch.setitem(CalibrationJob.__init__.__kwdefaults__, "progress_interval", 0.1)
    gui = make_gui(sim_count=3)
    _gather(gui, 3)
    seen: list[str | None] = []
    gui.fleet.bus.subscribe(lambda ev: seen.append(ev.busy) if isinstance(ev, BusyChanged) else None)
    gui.post("/api/sim/drop/3")
    _wait(lambda: gui.fleet.sessions[SIM3].state is LinkState.LOST)
    synced = gui.post("/api/time-sync", {"device_ids": [SIM1, SIM2, SIM3]})
    assert synced.status_code == 200
    items = {i["device_id"]: i for i in synced.json()["items"]}
    assert [i["device_id"] for i in synced.json()["items"]] == [SIM1, SIM2, SIM3]
    for index, device_id in enumerate((SIM1, SIM2)):
        written = items[device_id]["written_at"]
        assert written is not None and items[device_id]["error"] is None
        assert int.from_bytes(gui.sim.devices[index].tags[TAG_TIME_SYNC], "big") == int(written)
    assert items[SIM3] == {"device_id": SIM3, "written_at": None, "error": "not connected"}
    assert "apply" in seen  # other screens see it as a settings write

    assert gui.post("/api/time-sync", {"device_ids": ["0123"]}).status_code == 404
    assert gui.post("/api/batches", {"device_ids": [SIM1]}).status_code == 202
    refused = gui.post("/api/time-sync", {"device_ids": [SIM1]})
    assert refused.status_code == 409 and _code(refused) == "batch_active"


def test_a_slow_time_sync_does_not_hold_other_jobs(make_gui):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    gui.sim.devices[0].response_delay = 300.0  # 3 s of wall time before SIM1 answers the clock write
    result: list = []
    syncing = threading.Thread(target=lambda: result.append(gui.post("/api/time-sync", {"device_ids": [SIM1]})))
    syncing.start()
    try:
        _wait(lambda: gui.fleet.sessions[SIM1].busy == "apply")
        created = _write(gui, "/api/apply", _draft([SIM2], sensitivity=3))  # waited for the whole sync before
        assert created.status_code == 202
        assert syncing.is_alive()
        held = gui.post("/api/batches", {"device_ids": [SIM1]})  # the session lock still guards SIM1
        assert held.status_code == 409 and _code(held) == "busy"
    finally:
        syncing.join(timeout=20)
    assert result[0].status_code == 200  # the slow sensor may drop (missed keep-alives): only the lock matters
    assert result[0].json()["items"][0]["device_id"] == SIM1
    assert _done(gui, created)["items"][0]["state"] == "verified"


def test_calibration_history_newest_first(make_gui, monkeypatch):
    monkeypatch.setitem(CalibrationJob.__init__.__kwdefaults__, "progress_interval", 0.1)
    gui = make_gui(sim_count=1)
    gui.registry.add_site("lab-a", "Lab A")
    gui.registry.add_sensor(SIM1, "lab-a", "Sensor 1")
    gui.registry.match(SIM1, ADDR1)  # caches the address an old line names
    zones = [{"index": z, "distance_m": 0.8 * (z + 1), "trigger": 60 + z, "maintain": 30} for z in range(7)]
    gui.storage.append_history({"timestamp": "2026-01-01T00:00:00+00:00", "device_address": ADDR1, "zones": zones})
    gui.storage.append_history(
        {"timestamp": "2026-01-02T00:00:00+00:00", "device_id": SIM1, "sensitivity": 4, "zones": zones}
    )
    gui.storage.append_history({"timestamp": "2026-01-03T00:00:00+00:00", "device_id": SIM1, "zones": "broken"})

    got = gui.get(f"/api/sensors/{SIM1}/history")
    assert got.status_code == 200
    records = got.json()["records"]
    assert [r["timestamp"] for r in records] == ["2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]
    assert all(len(r["zones"]) == 7 for r in records) and records[0]["sensitivity"] == 4
    assert records[1]["sensitivity"] is None and records[1]["device_name"] is None
    assert gui.get("/api/sensors/0123/history").status_code == 404

    _gather(gui, 1)
    gui.sim.devices[0].calibration_secs = 60
    assert gui.post("/api/batches", {"device_ids": [SIM1]}).status_code == 202
    _wait(lambda: not _hub(gui).batches.active())
    newest = gui.get(f"/api/sensors/{SIM1}/history").json()["records"]
    assert len(newest) == 3 and newest[0]["timestamp"] > "2026-01-02"
    assert gui.post("/api/release", {"device_ids": [SIM1]}).status_code == 204
    assert gui.get(f"/api/sensors/{SIM1}/history").status_code == 200  # registered, no session


def test_settings_backups_list_and_detail(make_gui):
    gui = make_gui(sim_count=1)
    (device_id,) = _gather(gui, 1)
    assert gui.get(f"/api/sensors/{device_id}/snapshots").json() == {"device_id": device_id, "snapshots": []}
    final = _done(gui, _write(gui, "/api/apply", _draft([device_id], sensitivity=3)))
    name = final["items"][0]["snapshot"]
    listed = gui.get(f"/api/sensors/{device_id}/snapshots").json()["snapshots"]
    assert [(s["name"], s["reason"], s["sections"]) for s in listed] == [(name, "apply", ["sensitivity"])]
    detail = gui.get(f"/api/sensors/{device_id}/snapshots/{name}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["snapshot"]["name"] == name and body["profile"]["sensitivity"] == 2  # before the write
    assert body["profile"]["zone_thresholds"][0] == {"trigger": 95, "maintain": 40} and body["profile"]["dnd"] is None
    bad = gui.get(f"/api/sensors/{device_id}/snapshots/nope")
    assert bad.status_code == 422 and _code(bad) == "invalid_request"
    assert gui.get(f"/api/sensors/{device_id}/snapshots/20260101T000000000000Z").status_code == 404
    assert gui.get("/api/sensors/0123/snapshots").status_code == 404

    partial = gui.storage.save_snapshot(device_id, ConfigProfile(sensitivity=1), ["zone_distances"], "apply")
    lacking = gui.get(f"/api/sensors/{device_id}/snapshots/{partial.name}")  # not one the core writes
    assert lacking.status_code == 500 and _code(lacking) == "storage"
    unknown = gui.post("/api/rollback/preview", {"items": [{"device_id": device_id, "snapshot": partial.name}]})
    assert unknown.status_code == 422 and _code(unknown) == "invalid"


def test_device_history_is_read_once(make_gui):
    gui = make_gui(sim_count=2)
    _gather(gui, 2)
    dev = gui.sim.devices[0]
    empty = gui.get(f"/api/sensors/{SIM1}/device-history")
    assert empty.status_code == 200
    assert empty.json()["presence"] == [] and empty.json()["kind"] == "presence" and empty.json()["detail"] is False

    dev.tags[TAG_PRESENCE_HISTORY_COUNT] = (1).to_bytes(2, "big")
    dev.tags[TAG_PRESENCE_HISTORY_PUSH] = struct.pack(">HBBBI", 1, 0b001, 0x7F, 0b011, 1_700_000_000)
    presence = gui.get(f"/api/sensors/{SIM1}/device-history?kind=presence&detail=false").json()["presence"]
    assert len(presence) == 1
    record = presence[0]
    assert record["sensor_presence"] == [True, False, False] and record["zone_presence"][:2] == [True, True]
    assert record["zone_enabled"] == [True] * 7 and record["timestamp"] == 1_700_000_000
    assert record["sub_sensor_triggers"] == [] and record["zone_triggers"] == []

    dev.tags[TAG_LIGHT_HISTORY_COUNT] = (1).to_bytes(2, "big")
    dev.tags[TAG_LIGHT_HISTORY_PUSH] = struct.pack(">HIH", 1, 1_700_000_000, 120)
    light = gui.get(f"/api/sensors/{SIM1}/device-history?kind=light").json()
    assert light["presence"] == [] and light["light"] == [{"index": 1, "timestamp": 1_700_000_000, "light_lux": 120}]

    assert gui.get(f"/api/sensors/{SIM1}/device-history?kind=nope").status_code == 422
    gui.post("/api/sim/drop/2")
    _wait(lambda: gui.fleet.sessions[SIM2].state is LinkState.LOST)
    lost = gui.get(f"/api/sensors/{SIM2}/device-history")
    assert lost.status_code == 409 and _code(lost) == "not_connected"


def test_a_device_error_is_a_502(make_gui):
    gui = make_gui(sim_count=1)
    (device_id,) = _gather(gui, 1)
    gui.sim.devices[0].tags[TAG_PRESENCE_HISTORY_COUNT] = (1).to_bytes(2, "big")  # no tag58 to read back
    got = gui.get(f"/api/sensors/{device_id}/device-history")
    assert got.status_code == 502 and _code(got) == "device_error"
