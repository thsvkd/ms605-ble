"""ms605 gui live monitor: WS subscriptions hold exactly one session live
reference per watched sensor (G12), `live` messages (seq null, 7 zones, tag53
distances, 4 Hz per sensor), bad client frames, and the Client's latest-value
slots (docs/GUI_API.md 14.6.1-14.6.4). Every value is synthetic."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import recv_until

from ms605.events import LinkState
from ms605.gui.ws import Client
from ms605.protocol import TAG_LIVE_OUTPUT_ENABLE

SIM1 = "53494d3630350001"
SIM2 = "53494d3630350002"
TAG53_DISTANCES = [0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6]  # the simulator's tag53, unlike FALLBACK_DISTANCES_M


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def _subscribe(ws, *ids: str) -> None:
    ws.send_json({"type": "live_subscribe", "data": {"device_ids": list(ids)}})


def _unsubscribe(ws, *ids: str) -> None:
    ws.send_json({"type": "live_unsubscribe", "data": {"device_ids": list(ids)}})


def _live(device_id: str):
    return lambda m: m["type"] == "live" and m["data"]["device_id"] == device_id


def _gather(gui, *indices: int) -> None:
    gui.post("/api/gather/start")
    for index in indices:
        gui.post(f"/api/sim/press/{index}")
    ids = [f"53494d36303500{i:02x}" for i in indices]
    _wait(lambda: all(i in gui.fleet.sessions and gui.fleet.sessions[i].state is LinkState.CONNECTED for i in ids))


def _live_on(gui, index: int) -> bool:
    return gui.sim.devices[index - 1].tags[TAG_LIVE_OUTPUT_ENABLE] == b"\x01"


def _count(gui, device_id: str) -> int:
    return gui.fleet.sessions[device_id]._live


# -- through the app -------------------------------------------------------------------


@pytest.mark.timeout(30)
def test_subscription_is_one_session_reference(gui):
    _gather(gui, 1)
    with gui.ws() as a, gui.ws() as b:
        a.receive_json()
        b.receive_json()
        _subscribe(a, SIM1)
        _wait(lambda: _live_on(gui, 1) and _count(gui, SIM1) == 1)
        _subscribe(b, SIM1)
        _subscribe(b, SIM1)  # idempotent
        recv_until(b, _live(SIM1))
        assert _count(gui, SIM1) == 1  # one reference however many watch (G12)
        _unsubscribe(a, SIM1)
        recv_until(b, _live(SIM1))  # b still gets values
        assert _live_on(gui, 1) and _count(gui, SIM1) == 1
        _unsubscribe(b, SIM1)
        _wait(lambda: not _live_on(gui, 1) and _count(gui, SIM1) == 0)


@pytest.mark.timeout(30)
def test_closing_the_websocket_turns_live_output_off(gui):
    _gather(gui, 1)
    with gui.ws() as ws:
        ws.receive_json()
        _subscribe(ws, SIM1)
        recv_until(ws, _live(SIM1))
        assert _live_on(gui, 1)
    _wait(lambda: not _live_on(gui, 1) and _count(gui, SIM1) == 0)


@pytest.mark.timeout(30)
def test_subscribing_before_the_gather_and_no_leaked_references(gui):
    with gui.ws() as ws:
        ws.receive_json()
        _subscribe(ws, SIM1)  # not gathered yet: kept as an intention
        _gather(gui, 1)
        recv_until(ws, _live(SIM1))
        session = gui.fleet.sessions[SIM1]
        assert session._live == 1

        gui.post("/api/gather/stop")  # so the drop is not undone before it is seen
        gui.post("/api/sim/drop/1")
        _wait(lambda: session.state is LinkState.LOST)
        _gather(gui, 1)  # re-gather: the same session object comes back
        assert gui.fleet.sessions[SIM1] is session
        recv_until(ws, _live(SIM1))
        assert session._live == 1 and _live_on(gui, 1)

        gui.post("/api/gather/stop")
        assert gui.post("/api/release", {"device_ids": [SIM1]}).status_code == 204
        _wait(lambda: session._live == 0)  # the released session's reference was given back
        assert not _live_on(gui, 1)

        _gather(gui, 1)  # gathered again: a new session, still watched
        fresh = gui.fleet.sessions[SIM1]
        assert fresh is not session
        _wait(lambda: fresh._live == 1 and _live_on(gui, 1))
        assert session._live == 0


@pytest.mark.timeout(30)
def test_an_unexpected_error_turning_live_on_does_not_strand_the_reference(gui):
    """A non-MS605Error from the tag54 write must not end the reconcile task: the unsubscribe that
    came in meanwhile still gives the reference back."""
    _gather(gui, 1)
    session = gui.fleet.sessions[SIM1]
    real = session.ms.set_live_output

    async def driver_bug(on: bool) -> None:
        session.ms.set_live_output = real  # once
        await asyncio.sleep(0.2)
        raise RuntimeError("synthetic driver bug")

    session.ms.set_live_output = driver_bug
    with gui.ws() as ws:
        ws.receive_json()
        _subscribe(ws, SIM1)
        _wait(lambda: _count(gui, SIM1) == 1)  # the failing write is in flight
        _unsubscribe(ws, SIM1)
        _wait(lambda: _count(gui, SIM1) == 0 and SIM1 not in gui.app.state.hub.live.held)


@pytest.mark.timeout(30)
def test_live_message_content(gui):
    _gather(gui, 1)
    dev = gui.sim.devices[0]
    with gui.ws() as ws:
        ws.receive_json()
        _subscribe(ws, SIM1)
        message = recv_until(ws, _live(SIM1))[-1]
        assert message["seq"] is None
        data = message["data"]
        assert len(data["zones"]) == 7 and len(data["sub_sensor_presence"]) == 3
        assert [z["distance_m"] for z in data["zones"]] == pytest.approx(TAG53_DISTANCES)
        assert [z["index"] for z in data["zones"]] == list(range(7))
        zone = data["zones"][0]
        assert zone["trigger"] < zone["trigger_threshold"] and not zone["trigger_active"]

        dev.signal = [(5000, 5000)] * 7  # someone in the room
        hot = recv_until(ws, lambda m: _live(SIM1)(m) and m["data"]["zones"][0]["trigger"] > 1000)[-1]["data"]
        zone = hot["zones"][0]
        assert zone["trigger"] > zone["trigger_threshold"] and zone["trigger_active"]
        assert zone["maintain"] > zone["maintain_threshold"]
        assert hot["sub_sensor_presence"] == [True, True, True]


@pytest.mark.timeout(30)
def test_live_is_throttled_to_4_hz(gui):
    _gather(gui, 1)  # speed 100: tag55 at 100 Hz
    with gui.ws() as ws:
        ws.receive_json()
        _subscribe(ws, SIM1)
        first = recv_until(ws, _live(SIM1))[-1]
        got = recv_until(ws, lambda m: m["ts"] > first["ts"] + 1.0)
        window = [m for m in [first, *got] if _live(SIM1)(m) and m["ts"] <= first["ts"] + 1.0]
        assert 2 <= len(window) <= 6


@pytest.mark.timeout(30)
def test_unsubscribed_clients_get_no_live_and_an_unbroken_seq(gui):
    _gather(gui, 1, 2)
    with gui.ws() as watcher, gui.ws() as other:
        other_snapshot = other.receive_json()
        watcher.receive_json()
        _subscribe(watcher, SIM1)
        recv_until(watcher, _live(SIM1))
        gui.post("/api/sites", {"name": "Lab A"})  # one ordered message for everyone
        gui.post("/api/sensors", {"device_id": SIM2, "site_id": "lab-a", "alias": "Sensor 2"})
        got = recv_until(other, lambda m: m["type"] == "sensor" and m["data"]["registry"] is not None)
        assert not [m for m in got if m["type"] == "live"]
        seqs = [m["seq"] for m in got]
        assert seqs == list(range(other_snapshot["seq"] + 1, other_snapshot["seq"] + 1 + len(seqs)))


@pytest.mark.timeout(30)
def test_bad_client_frames_are_dropped_and_the_connection_kept(gui):
    _gather(gui, 1)
    with gui.ws() as ws:
        ws.receive_json()
        ws.send_text('{"type":"nope"}')
        ws.send_text("{not json")
        ws.send_text(json.dumps({"type": "live_subscribe", "data": {"device_ids": [SIM1]}, "pad": "x" * 4097}))
        ws.send_bytes(b"\x00\x01")
        _unsubscribe(ws, "XYZ")  # not a device id: invalid, dropped
        time.sleep(0.1)
        assert _count(gui, SIM1) == 0  # the oversized subscribe was not applied
        _subscribe(ws, SIM1)
        assert recv_until(ws, _live(SIM1))[-1]["data"]["device_id"] == SIM1


# -- Client slots on their own ---------------------------------------------------------


class StuckWS:
    """send_text() records the text, after waiting for `gate`."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: int | None = None
        self.gate = asyncio.Event()

    async def send_text(self, text: str) -> None:
        await self.gate.wait()
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def _stuck_client(maxsize: int = 512) -> tuple[Client, StuckWS, asyncio.Task]:
    ws = StuckWS()
    client = Client(ws, maxsize)
    task = asyncio.create_task(client.run())
    client.put("q0")
    await asyncio.sleep(0.01)  # the sender is now stuck sending q0
    return client, ws, task


async def _release(client: Client, ws: StuckWS, task: asyncio.Task) -> list[str]:
    ws.gate.set()
    await asyncio.sleep(0.01)
    client.close(1000)
    await asyncio.wait_for(task, 1)
    return ws.sent


def test_slot_keeps_only_the_latest_value():
    async def main():
        client, ws, task = await _stuck_client()
        for i in range(50):
            client.put_slot("live:a", f"a{i}")
        assert await _release(client, ws, task) == ["q0", "a49"]

    asyncio.run(main())


def test_slots_never_overflow_the_queue():
    async def main():
        client, ws, task = await _stuck_client(maxsize=8)
        for i in range(100):
            client.put_slot(f"live:{i}", f"v{i}")
        assert client.close_code is None  # no 1013
        assert len(client.slots) == 100
        sent = await _release(client, ws, task)
        assert ws.closed == 1000 and sent[0] == "q0"

    asyncio.run(main())


def test_queued_messages_go_before_slots_and_keys_take_turns():
    async def main():
        client, ws, task = await _stuck_client()
        client.put_slot("live:a", "a1")
        client.put_slot("live:b", "b1")
        client.put("q1")
        client.put_slot("live:a", "a2")  # overwritten: moves behind b
        ws.gate.set()
        await asyncio.sleep(0.01)
        assert ws.sent == ["q0", "q1", "b1", "a2"]
        client.drop_slot("live:a")
        client.close(1000)
        await asyncio.wait_for(task, 1)

    asyncio.run(main())


def test_put_slot_after_close_is_ignored():
    async def main():
        client, ws, task = await _stuck_client()
        client.put_slot("live:a", "a1")
        client.close(1001)
        assert client.slots == {}
        client.put_slot("live:a", "a2")
        assert client.slots == {}
        ws.gate.set()
        await asyncio.wait_for(task, 1)
        assert ws.sent == ["q0"] and ws.closed == 1001

    asyncio.run(main())
