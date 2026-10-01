"""ms605 gui /ws: snapshot then seq-numbered view messages, multi-client
consistency (D11), backpressure and the hub's event mapping
(docs/GUI_API.md section 7). Every value is synthetic."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import GUI_AUTH, recv_until

import ms605.events as events_mod
from ms605.events import (
    BusyChanged,
    DeviceEvent,
    Event,
    GatherFailed,
    HandlerFailed,
    LinkState,
    LinkStateChanged,
    PirChanged,
)
from ms605.fleet import Fleet
from ms605.gui import ws as ws_mod
from ms605.gui.schemas import ServerInfo
from ms605.gui.ws import HANDLED_EVENTS, IGNORED_EVENTS, Hub
from ms605.models import ConfigProfile
from ms605.registry import Registry
from ms605.sim import SimFleet
from ms605.storage import Storage

SIM1 = "53494d3630350001"
SIM2 = "53494d3630350002"
ADDR1 = "02:00:00:00:00:01"


def _is(kind: str, device_id: str | None = None, check=lambda d: True):
    def predicate(m: dict) -> bool:
        if m["type"] != kind:
            return False
        if device_id is not None and m["data"].get("device_id") != device_id:
            return False
        return check(m["data"])

    return predicate


def _connected(m: dict) -> bool:
    return m["type"] == "sensor" and m["data"]["live"] is not None and m["data"]["live"]["link"] == "connected"


def _assert_consecutive(messages: list[dict], after: int) -> None:
    messages = [m for m in messages if m["seq"] is not None]  # live / countdown carry no seq (14.6.3)
    assert [m["seq"] for m in messages] == list(range(after + 1, after + 1 + len(messages)))


# -- through the app ------------------------------------------------------------------


@pytest.mark.timeout(30)
def test_gather_flow_and_naming(gui):
    with gui.ws() as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["seq"] == snapshot["data"]["seq"] == 0
        assert snapshot["data"]["sensors"] == []

        assert gui.post("/api/gather/start").status_code == 200
        assert gui.post("/api/sim/press/1").status_code == 204
        got = recv_until(ws, _is("sensor", SIM1, lambda d: d["live"]["link"] == "connected"))
        got += recv_until(ws, _is("gather"))
        kinds = [m["type"] for m in got]
        assert kinds[0] == "gather" and got[0]["data"]["gathering"] is True  # from gather/start
        connecting = [m for m in got if m["type"] == "gather" and m["data"]["connecting"]]
        assert connecting[0]["data"]["connecting"][0]["address"] == ADDR1
        assert got.index(connecting[0]) < kinds.index("sensor")
        sensor = next(m for m in got if _connected(m))["data"]
        assert sensor["registry"] is None
        assert sensor["live"]["name"] == "MRBL_SIM01" and sensor["live"]["firmware"] == "1.0.0"
        assert got[-1]["data"]["connecting"] == []

        assert gui.post("/api/sites", {"name": "Lab A"}).status_code == 201
        created = gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "Sensor 1"})
        assert created.status_code == 201
        got += recv_until(ws, _is("sensor", SIM1, lambda d: d["registry"] is not None))
        assert got[-1]["data"]["registry"]["alias"] == "Sensor 1"
        assert got[-1]["data"]["live"]["link"] == "connected"
        assert any(m["type"] == "sites" and m["data"]["sites"][0]["site_id"] == "lab-a" for m in got)
        _assert_consecutive(got, 0)
    assert gui.registry.sensors[SIM1].addresses  # match() cached the address on gather


@pytest.mark.timeout(30)
def test_two_clients_see_the_same_stream(gui):
    with gui.ws() as a, gui.ws() as b:
        snap_a, snap_b = a.receive_json(), b.receive_json()
        assert snap_a["seq"] == snap_b["seq"] == 0
        gui.post("/api/gather/start")
        gui.post("/api/sim/press/1")
        gui.post("/api/sim/press/2")
        got_a = recv_until(a, _is("sensor", SIM1, lambda d: d["live"]["link"] == "connected"))
        got_a += recv_until(a, _is("sensor", SIM2, lambda d: d["live"]["link"] == "connected"))
        gui.post("/api/gather/stop")
        gui.post("/api/sites", {"name": "Lab A"})
        gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "Sensor 1"})
        got_a += recv_until(a, _is("sensor", SIM1, lambda d: d["registry"] is not None))
        last = got_a[-1]["seq"]
        got_b = recv_until(b, lambda m: m["seq"] == last)
        assert [(m["seq"], m["type"], m["data"]) for m in got_a] == [(m["seq"], m["type"], m["data"]) for m in got_b]
        _assert_consecutive(got_a, 0)

        with gui.ws() as late:
            snap = late.receive_json()
            assert snap["seq"] == last
            sensors = {s["device_id"]: s for s in snap["data"]["sensors"]}
            assert sensors[SIM1]["registry"]["alias"] == "Sensor 1"
            assert sensors[SIM2]["registry"] is None and sensors[SIM2]["live"]["link"] == "connected"
            assert snap["data"]["sites"] == [{"site_id": "lab-a", "name": "Lab A"}]
            gui.client.patch(f"/api/sensors/{SIM1}", json={"location": "door"}, headers=GUI_AUTH)
            for ws in (late, a, b):
                message = ws.receive_json()
                assert message["seq"] == last + 1
                assert message["data"]["registry"]["location"] == "door"


@pytest.mark.timeout(30)
def test_drop_and_release(gui):
    with gui.ws() as ws:
        ws.receive_json()
        gui.post("/api/gather/start")
        gui.post("/api/sim/press-all")
        recv_until(ws, _is("sensor", SIM1, lambda d: d["live"]["link"] == "connected"))
        recv_until(ws, _is("sensor", SIM2, lambda d: d["live"]["link"] == "connected"))
        gui.post("/api/sites", {"name": "Lab A"})
        gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "Sensor 1"})

        assert gui.post("/api/sim/drop/1").status_code == 204
        lost = recv_until(ws, _is("sensor", SIM1, lambda d: d["live"]["link"] == "lost"))[-1]["data"]
        assert lost["live"]["lost_reason"]

        gui.post("/api/gather/stop")
        assert gui.post("/api/release", {"device_ids": [SIM2]}).status_code == 204
        recv_until(ws, _is("sensor_removed", SIM2))
        assert gui.post("/api/release", {"device_ids": [SIM1]}).status_code == 204
        released = recv_until(ws, _is("sensor", SIM1, lambda d: d["live"] is None))[-1]["data"]
        assert released["registry"]["alias"] == "Sensor 1"


@pytest.mark.timeout(30)
def test_import_and_pending_resolution(gui):
    with gui.ws() as ws:
        ws.receive_json()
        body = {"filename": "sensor_info_example.yaml", "content": f"Door: {ADDR1}\n"}
        assert gui.post("/api/import/sensor-info", body).status_code == 200
        sites = ws.receive_json()
        pending = ws.receive_json()
        assert sites["type"] == "sites" and sites["data"]["sites"][0]["site_id"] == "example"
        assert pending["type"] == "pending" and pending["data"]["pending"][0]["alias"] == "Door"
        gui.post("/api/gather/start")
        gui.post("/api/sim/press/1")
        got = recv_until(ws, _is("sensor", SIM1, lambda d: d["registry"] is not None))
        assert got[-1]["data"]["registry"]["alias"] == "Door"
        assert any(m["type"] == "pending" and m["data"]["pending"] == [] for m in got)


@pytest.mark.timeout(30)
def test_shutdown_releases_every_link(make_gui):
    rig = make_gui(enter=False)
    with rig.client:
        rig.post("/api/gather/start")
        rig.post("/api/sim/press-all")
        with rig.ws() as ws:
            ws.receive_json()
            for device_id in (SIM1, SIM2, "53494d3630350003"):
                recv_until(ws, _is("sensor", device_id, lambda d: d["live"]["link"] == "connected"))
        assert all(dev.connected for dev in rig.sim.devices)
    assert not any(dev.connected for dev in rig.sim.devices)
    assert not rig.fleet.sessions and not rig.fleet.gathering


# -- the hub on its own --------------------------------------------------------------


class FakeWS:
    """send_text() records; with `blocked`, it waits until `gate` is set."""

    def __init__(self, blocked: bool = False) -> None:
        self.sent: list[dict] = []
        self.closed: int | None = None
        self.gate = asyncio.Event()
        if not blocked:
            self.gate.set()

    async def send_text(self, text: str) -> None:
        await self.gate.wait()
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def receive(self) -> dict:
        await asyncio.Event().wait()  # the peer never sends or leaves
        raise AssertionError


def _hub(tmp_path, queue_size: int = 512) -> Hub:
    storage = Storage(root=tmp_path)
    seam = SimFleet(0)
    fleet = Fleet(Registry(storage), storage, scan=seam.discover, client_factory=seam.client_factory)
    return Hub(fleet, ServerInfo(version="0", lan=False, sim=None), queue_size=queue_size)


def _drain_queue(hub: Hub) -> list[dict]:
    (client,) = hub.clients
    out = []
    while not client.queue.empty():
        out.append(json.loads(client.queue.get_nowait()))
    return out


def test_full_queue_closes_only_that_client(tmp_path):
    async def main() -> None:
        hub = _hub(tmp_path, queue_size=4)
        hub.attach()
        slow, fast = FakeWS(blocked=True), FakeWS()
        tasks = [asyncio.create_task(hub.serve(slow)), asyncio.create_task(hub.serve(fast))]
        await asyncio.sleep(0.01)  # both senders took their snapshot off the queue
        for _ in range(10):
            hub.mark_gather()
            hub.flush()
            await asyncio.sleep(0)  # the fast sender keeps up; the slow one is stuck on its first send
        assert fast.closed is None
        assert [m["seq"] for m in fast.sent] == list(range(11))
        slow.gate.set()  # the stuck send returns; the sender sees the close mark next
        await asyncio.wait_for(tasks[0], 1)
        assert slow.closed == 1013
        assert [m["seq"] for m in slow.sent] == [0]  # what was queued was dropped, not sent
        assert len(hub.clients) == 1
        hub.mark_gather()
        hub.flush()
        await asyncio.sleep(0.01)
        assert fast.sent[-1]["seq"] == 11
        hub.close_clients()
        await asyncio.wait_for(tasks[1], 1)
        assert fast.closed == 1001

    asyncio.run(main())


def test_stuck_send_closes_1013(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_mod, "SEND_TIMEOUT_S", 0.05)

    async def main() -> None:
        hub = _hub(tmp_path)
        stuck = FakeWS(blocked=True)
        await asyncio.wait_for(hub.serve(stuck), 1)
        assert stuck.closed == 1013 and not hub.clients

    asyncio.run(main())


def test_same_notice_is_suppressed_for_ten_seconds(tmp_path):
    async def main() -> None:
        hub = _hub(tmp_path)
        hub.attach()
        hub.add_client(FakeWS())
        _drain_queue(hub)  # the snapshot
        bus = hub.fleet.bus
        for at, address in ((1000.0, ADDR1), (1005.0, ADDR1), (1005.0, "02:00:00:00:00:02"), (1010.5, ADDR1)):
            bus.emit(GatherFailed(at=at, address=address, device_id=None, name="MRBL_SIM01", error="timed out"))
        bus.emit(HandlerFailed(at=1000.0, event_type="X", handler="h", error="boom"))
        await asyncio.sleep(0)
        notices = [m["data"] for m in _drain_queue(hub) if m["type"] == "notice"]
        assert [(n["at"], n["address"], n["code"]) for n in notices] == [
            (1000.0, ADDR1, "gather_failed"),
            (1005.0, "02:00:00:00:00:02", "gather_failed"),
            (1010.5, ADDR1, "gather_failed"),
            (1000.0, None, "internal"),
        ]
        assert notices[0] == {
            "level": "warning", "code": "gather_failed", "message": "timed out", "device_id": None,
            "address": ADDR1, "name": "MRBL_SIM01", "at": 1000.0,
        }  # fmt: skip
        assert notices[3]["level"] == "error" and notices[3]["message"] == "X: boom"

    asyncio.run(main())


def test_events_of_one_tick_coalesce(tmp_path):
    async def main() -> None:
        hub = _hub(tmp_path)
        hub.registry.add_site("lab-a", "Lab A")
        hub.registry.add_sensor(SIM1, "lab-a", "Sensor 1")
        hub.attach()
        hub.add_client(FakeWS())
        _drain_queue(hub)
        bus = hub.fleet.bus
        for state, previous in ((LinkState.CONNECTING, LinkState.LOST), (LinkState.CONNECTED, LinkState.CONNECTING)):
            bus.emit(LinkStateChanged(address=ADDR1, device_id=SIM1, state=state, previous=previous))
        bus.emit(PirChanged(address=ADDR1, device_id=SIM1, detected=True))  # nobody watches: no live
        bus.emit(BusyChanged(address=ADDR1, device_id=SIM1, busy="read"))  # no session: ignored
        await asyncio.sleep(0)
        messages = _drain_queue(hub)
        assert [(m["seq"], m["type"]) for m in messages] == [(1, "sensor")]
        assert messages[0]["data"]["registry"]["alias"] == "Sensor 1"

    asyncio.run(main())


def test_connecting_list(tmp_path):
    async def main() -> None:
        hub = _hub(tmp_path)
        hub.attach()
        bus = hub.fleet.bus
        bus.emit(
            LinkStateChanged(
                at=5.0, address=ADDR1, device_id=None, state=LinkState.CONNECTING, previous=LinkState.DISCONNECTED
            )
        )
        await asyncio.sleep(0)
        assert hub.gather_status().model_dump()["connecting"] == [{"address": ADDR1, "since": 5.0}]
        bus.emit(
            LinkStateChanged(
                address=ADDR1, device_id=None, state=LinkState.CONNECTED, previous=LinkState.CONNECTING
            )
        )
        assert hub.gather_status().connecting  # still identifying
        bus.emit(
            LinkStateChanged(
                address=ADDR1.lower(), device_id=None, state=LinkState.DISCONNECTED, previous=LinkState.CONNECTED
            )
        )
        assert hub.gather_status().connecting == []

    asyncio.run(main())


def test_event_mapping_covers_every_event_class():
    classes = {c for c in vars(events_mod).values() if isinstance(c, type) and issubclass(c, Event)}
    classes -= {Event, DeviceEvent}
    assert not HANDLED_EVENTS & IGNORED_EVENTS
    assert HANDLED_EVENTS | IGNORED_EVENTS == classes


def test_sensor_view_reads_history_and_snapshots(tmp_path):
    hub = _hub(tmp_path)
    hub.registry.add_site("lab-a", "Lab A")
    hub.registry.add_sensor(SIM1, "lab-a", "Sensor 1")
    storage = hub.storage
    # an old line written before device ids existed counts only through a cached address: none here
    storage.append_history({"timestamp": "2026-01-01T00:00:00+00:00", "device_address": ADDR1, "sensitivity": 1})
    storage.append_history({"timestamp": "2026-01-02T00:00:00+00:00", "device_id": SIM1, "sensitivity": 2})
    storage.append_history({"timestamp": "2026-01-03T00:00:00+00:00", "device_id": SIM2, "sensitivity": 3})
    storage.save_snapshot(SIM1, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    newest = storage.save_snapshot(SIM1, ConfigProfile(sensitivity=2), ["sensitivity"], "rollback")
    view = hub.sensor_view(SIM1)
    assert view.last_calibration.model_dump() == {
        "timestamp": "2026-01-02T00:00:00+00:00", "sensitivity": 2, "detect_mode": None,
    }  # fmt: skip
    assert view.last_snapshot.model_dump() == {"name": newest.name, "taken_at": newest.taken_at, "reason": "rollback"}
    assert hub.sensor_view(SIM2) is None  # neither registered nor connected
    (storage.snapshots_dir / SIM1 / f"{newest.name}.json").write_text("{broken", encoding="utf-8")
    assert hub.sensor_view(SIM1).last_snapshot is None


def test_history_is_parsed_once_per_change(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    hub.registry.add_site("lab-a", "Lab A")
    hub.registry.add_sensor(SIM1, "lab-a", "Sensor 1")
    hub.registry.add_sensor(SIM2, "lab-a", "Sensor 2")
    hub.registry.match(SIM2, ADDR1)  # SIM2 has an old pre-device-id line through its cached address
    storage = hub.storage
    storage.append_history({"timestamp": "2026-01-01T00:00:00+00:00", "device_address": ADDR1, "sensitivity": 1})
    storage.append_history({"timestamp": "2026-01-02T00:00:00+00:00", "device_id": SIM1, "sensitivity": 2})
    reads = []
    real_read = storage.read_history
    monkeypatch.setattr(storage, "read_history", lambda *a, **kw: reads.append(a) or real_read(*a, **kw))

    snapshot = hub.snapshot()
    hub.snapshot()
    assert len(reads) == 1  # one parse for every view of both snapshots
    calibration = {s.device_id: s.last_calibration.sensitivity for s in snapshot.sensors}
    assert calibration == {SIM1: 2, SIM2: 1}

    storage.append_history({"timestamp": "2026-01-03T00:00:00+00:00", "device_id": SIM2, "sensitivity": 3})
    assert hub.sensor_view(SIM2).last_calibration.sensitivity == 3  # the file changed: parsed again
    assert hub.sensor_view(SIM1).last_calibration.sensitivity == 2
    assert len(reads) == 2
