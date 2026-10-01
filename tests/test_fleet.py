"""ms605.fleet against a SimFleet of several virtual devices: continuous
gathering, batch calibration (preflight, now / after N s / at a time, a link
dropped mid-run, retrying only the failed ones), drafts (relative and absolute
thresholds, validation before any write, polled verify under the simulator's
apply_delay, per-device partial failure, rollback) and clone. Every value here
is synthetic."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import ms605.fleet as fleet_mod
from ms605 import ConfigProfile, MS605ConnectionError, MS605Error, ProfileError, StorageError
from ms605.events import (
    ApplyResult,
    ApplyStatus,
    BatchChanged,
    BatchState,
    BusyChanged,
    CalibrationState,
    GatherFailed,
    LinkState,
    LinkStateChanged,
    SensorGathered,
)
from ms605.fleet import Draft, Fleet, SensorChanges, ThresholdChange, apply_changes, poll_verify
from ms605.models import PROFILE_SECTION_KEYS, encode_zone_thresholds
from ms605.protocol import (
    SENSITIVITY_PRESETS,
    TAG_DETECT_MODE,
    TAG_DEVICE_ID,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    DetectMode,
    Sensitivity,
)
from ms605.registry import Registry
from ms605.session import DeviceSession
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet, SimMS605
from ms605.storage import Storage

pytestmark = pytest.mark.usefixtures("no_chunk_pacing")

SPEED = 100.0
KEEPALIVE = 15 / SPEED  # the real 15 s interval, scaled like the 30 s idle drop (0.3 s)
TIMEOUT = 200 / SPEED
MEDIUM = list(zip(*SENSITIVITY_PRESETS[Sensitivity.MEDIUM], strict=True))
S = CalibrationState


def _make(tmp_path, count: int, *, connect_delay: float = 0.0, disconnect_delay: float = 0.0, **sim_kw):
    """`connect_delay`: wall seconds every BLE connect takes (an in-flight connect to race).
    `disconnect_delay`: wall seconds a BLE disconnect takes to return after the device is free."""
    sim_kw.setdefault("speed", SPEED)
    sim_kw.setdefault("calibration_secs", 20)  # 0.2 s of wall time
    # tag51 read-back lag off by default, so a write's verify needs no 0.4 s re-poll;
    # the tests of the polled verify itself set apply_delay
    sim_kw.setdefault("apply_delay", None)
    sim = SimFleet(count, **sim_kw)
    storage = Storage(root=tmp_path / "data")

    def client_factory(*args, **kwargs):
        client = sim.client_factory(*args, **kwargs)
        if connect_delay:
            real_connect = client.connect

            async def slow_connect(**kw):
                await asyncio.sleep(connect_delay)
                return await real_connect(**kw)

            client.connect = slow_connect
        if disconnect_delay:
            real_disconnect = client.disconnect

            async def slow_disconnect():
                result = await real_disconnect()
                await asyncio.sleep(disconnect_delay)
                return result

            client.disconnect = slow_disconnect
        return client

    fleet = Fleet(
        Registry(storage, host="host-1"),
        storage,
        scan=sim.discover,
        client_factory=client_factory,
        keepalive_interval=KEEPALIVE,
        gather_pause=0.01,
    )
    events: list = []
    fleet.bus.subscribe(events.append)
    return sim, fleet, events


def _id(dev) -> str:
    return dev.tags[TAG_DEVICE_ID].hex()


async def _gather_all(sim, fleet) -> list[str]:
    sim.press_all()
    for dev in sim.devices:
        await fleet.connect(dev.ble_device)
    return [_id(dev) for dev in sim.devices]


def _of(events, kind) -> list:
    return [e for e in events if isinstance(e, kind)]


def _writes(dev, tag: int) -> list[bytes]:
    return [v for f in dev.frames_in for t, v in f.attributes if t == tag]


async def _until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


# -- gathering ---------------------------------------------------------------------


def test_gather_connects_sensors_as_their_buttons_are_pressed(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 4)
        d1, d2, d3, d4 = sim.devices
        fleet.registry.add_site("lab-a", "Lab A")
        fleet.registry.add_sensor(_id(d1), "lab-a", "Sensor 1")
        info = tmp_path / "sensor_info_example.yaml"
        info.write_text(f"Sensor 2: {d2.address}\n", encoding="utf-8")
        fleet.registry.import_sensor_info(info, "lab-a")
        async with fleet:
            fleet.start_gather()
            fleet.start_gather()  # already running: no second loop
            assert fleet.gathering
            await asyncio.sleep(0.05)
            assert not fleet.sessions  # nothing advertises before a press
            for dev in (d1, d2, d3):  # pressed one after another
                dev.press_button()
                await _until(lambda dev=dev: _id(dev) in fleet.sessions)

            gathered = {e.device_id: e for e in _of(events, SensorGathered)}
            assert len(_of(events, SensorGathered)) == 3
            known = gathered[_id(d1)]
            assert (known.known, known.site_id, known.alias, known.resolved_pending) == (
                True,
                "lab-a",
                "Sensor 1",
                False,
            )
            assert known.address == d1.address and known.name == d1.name
            pending = gathered[_id(d2)]
            assert (pending.known, pending.alias, pending.resolved_pending) == (True, "Sensor 2", True)
            new = gathered[_id(d3)]
            assert (new.known, new.site_id, new.alias, new.resolved_pending) == (False, None, None, False)
            assert _id(d2) in fleet.registry.sensors and not fleet.registry.pending
            assert _id(d3) not in fleet.registry.sensors  # registering a new sensor is the UI's call
            assert fleet.registry.address_for(_id(d1)) == d1.address

            # keep-alive holds every link while gathering goes on (idle drop is 0.3 s)
            await asyncio.sleep(0.4)
            assert all(s.state is LinkState.CONNECTED for s in fleet.sessions.values())
            assert d1.connected and d2.connected and d3.connected and not d4.connected

            await fleet.stop_gather()
            assert not fleet.gathering
            d4.press_button()
            await asyncio.sleep(0.1)
            assert not d4.connected and _id(d4) not in fleet.sessions
        assert not fleet.sessions and not any(dev.connected for dev in sim.devices)

    asyncio.run(main())


def test_a_pending_import_matches_an_address_that_differs_only_in_case(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 0)
        dev = SimMS605(3, address="02:AA:BB:00:00:03", speed=SPEED, apply_delay=None)  # synthetic, has letters
        sim.devices.append(dev)
        fleet.registry.add_site("lab-a", "Lab A")
        info = tmp_path / "sensor_info_example.yaml"
        info.write_text(f"Sensor 3: {dev.address.lower()}\n", encoding="utf-8")  # 02:aa:bb:00:00:03
        fleet.registry.import_sensor_info(info, "lab-a")
        dev.press_button()
        async with fleet:
            await fleet.connect(dev.ble_device)
            (gathered,) = _of(events, SensorGathered)
            assert (gathered.known, gathered.alias, gathered.resolved_pending) == (True, "Sensor 3", True)
            assert not fleet.registry.pending
            assert fleet.registry.address_for(_id(dev)) == "02:AA:BB:00:00:03"  # the address the link used

    asyncio.run(main())


def test_gather_accept_filter_skips_other_sensors(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 2)
        d1, d2 = sim.devices
        sim.press_all()
        async with fleet:
            fleet.start_gather(accept=lambda dev: dev.address == d2.address)
            await _until(lambda: _id(d2) in fleet.sessions)
            await asyncio.sleep(0.05)
            assert list(fleet.sessions) == [_id(d2)] and not d1.connected

    asyncio.run(main())


def test_failed_identification_is_reported_and_retried_on_the_next_scan(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1)
        dev = sim.devices[0]
        device_id = _id(dev)
        dev.tags[TAG_DEVICE_ID] = b""  # tag30 comes back empty
        async with fleet:
            dev.press_button()
            fleet.start_gather()
            await _until(lambda: len(_of(events, GatherFailed)) >= 2)  # retried on later scans
            failed = _of(events, GatherFailed)[0]
            assert failed.address == dev.address and failed.name == dev.name and failed.device_id is None
            assert "tag 30" in failed.error
            assert not fleet.sessions and not _of(events, SensorGathered)

            dev.tags[TAG_DEVICE_ID] = bytes.fromhex(device_id)
            await _until(lambda: device_id in fleet.sessions)
            assert fleet.sessions[device_id].state is LinkState.CONNECTED

    asyncio.run(main())


def test_lost_sensor_comes_back_into_its_own_session_when_pressed_again(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1, connectable_window=20)  # a 0.2 s window
        dev = sim.devices[0]
        async with fleet:
            fleet.start_gather()
            dev.press_button()
            await _until(lambda: _id(dev) in fleet.sessions)
            session = fleet.sessions[_id(dev)]
            await asyncio.sleep(0.3)  # the window closes; keep-alive holds the link
            assert session.state is LinkState.CONNECTED

            dev.drop_link()
            await _until(lambda: session.state is LinkState.LOST)
            await asyncio.sleep(0.1)
            assert session.state is LinkState.LOST  # no advertising without a press

            dev.press_button()
            # re-identified and reported like a first gather, so the UI shows it connected again
            await _until(lambda: len(_of(events, SensorGathered)) == 2)
            assert fleet.sessions[_id(dev)] is session and dev.connected  # a return, not a new session
            assert session.state is LinkState.CONNECTED
            back = _of(events, SensorGathered)[1]
            assert (back.device_id, back.address, back.name) == (_id(dev), dev.address, dev.name)

    asyncio.run(main())


def test_a_regathered_address_answering_with_another_device_id_is_refused(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1, connectable_window=20)  # a 0.2 s window
        dev = sim.devices[0]
        device_id = _id(dev)
        async with fleet:
            fleet.start_gather()
            dev.press_button()
            await _until(lambda: device_id in fleet.sessions)
            session = fleet.sessions[device_id]
            await asyncio.sleep(0.3)  # the window closes, so nothing reconnects before the id changes
            dev.drop_link()
            await _until(lambda: session.state is LinkState.LOST)

            dev.tags[TAG_DEVICE_ID] = b"SIM605XX"  # synthetic: a different sensor now answers at this address
            dev.press_button()
            await _until(lambda: _of(events, GatherFailed))
            failed = _of(events, GatherFailed)[0]
            assert failed.device_id == device_id and "device id changed" in failed.error
            # stopped first: while the button window is open the gather loop may be trying the address again
            await fleet.stop_gather()
            assert session.device_id == device_id and session.info.device_id == device_id
            assert fleet.sessions[device_id] is session and session.state is not LinkState.CONNECTED
            assert len(_of(events, SensorGathered)) == 1
            assert not dev.connected

    asyncio.run(main())


def test_a_regather_that_finds_the_lock_taken_keeps_the_link_for_its_holder(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1, connectable_window=20)  # a 0.2 s window
        dev = sim.devices[0]
        device_id = _id(dev)
        async with fleet:
            fleet.start_gather()
            dev.press_button()
            await _until(lambda: device_id in fleet.sessions)
            session = fleet.sessions[device_id]
            await session.acquire_live()  # a live view is open: the reconnect re-enables tag54 after CONNECTED
            await asyncio.sleep(0.3)  # the window closes, so nothing reconnects before the press below
            dev.drop_link()
            await _until(lambda: session.state is LinkState.LOST)

            # a GUI-style subscriber starts a read as soon as the sensor shows CONNECTED again
            free = asyncio.Event()
            reads: list = []

            async def operator_read():
                async with session.operation("read") as ms:
                    await free.wait()
                    reads.append(await ms.read_raw([TAG_DEVICE_ID]))

            def on_event(event):
                if isinstance(event, LinkStateChanged) and event.state is LinkState.CONNECTED and not reads:
                    reads.append(asyncio.create_task(operator_read()))

            fleet.bus.subscribe(on_event)
            dev.press_button()
            await _until(lambda: _of(events, GatherFailed))
            failed = _of(events, GatherFailed)[0]
            assert failed.device_id == device_id and "busy" in failed.error
            # the healthy link stays up for the operation that holds it
            assert session.state is LinkState.CONNECTED and dev.connected and session.busy == "read"
            free.set()
            await reads[0]
            assert reads[1].get(TAG_DEVICE_ID) == bytes.fromhex(device_id)
            assert session.state is LinkState.CONNECTED and dev.connected
            assert len(_of(events, GatherFailed)) == 1  # a live session is not regathered again
            await session.release_live()

    asyncio.run(main())


def test_duplicate_device_id_is_refused_while_live_and_replaces_a_lost_session(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 2)
        d1, d2 = sim.devices
        d2.tags[TAG_DEVICE_ID] = d1.tags[TAG_DEVICE_ID]
        sim.press_all()
        async with fleet:
            first = await fleet.connect(d1.ble_device)
            with pytest.raises(MS605Error, match="duplicate device id"):
                await fleet.connect(d2.ble_device)
            assert dict(fleet.sessions) == {_id(d1): first} and not d2.connected

            d1.drop_link()
            await _until(lambda: first.state is LinkState.LOST)
            second = await fleet.connect(d2.ble_device)  # the same sensor at a new address
            assert fleet.sessions[_id(d1)] is second and second.address == d2.address
            assert first.state is LinkState.DISCONNECTED

    asyncio.run(main())


def test_release_closes_only_the_given_sessions(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 2)
        ids = await _gather_all(sim, fleet)
        with pytest.raises(KeyError):
            await fleet.release(["0" * 16])
        await fleet.release([ids[0]])
        assert list(fleet.sessions) == [ids[1]]
        assert not sim.devices[0].connected and sim.devices[1].connected
        await fleet.aclose()
        assert not fleet.sessions and not sim.devices[1].connected

    asyncio.run(main())


def test_release_while_gathering_never_leaves_a_reconnected_session_behind(tmp_path):
    async def main():
        # the device is free (and still advertising) while release() waits for the disconnect
        sim, fleet, _ = _make(tmp_path, 1, disconnect_delay=0.2)
        dev = sim.devices[0]
        dev.press_button()
        async with fleet:
            fleet.start_gather()
            await _until(lambda: _id(dev) in fleet.sessions)
            session = fleet.sessions[_id(dev)]
            await fleet.release([_id(dev)])
            await fleet.stop_gather()
            assert session.state is LinkState.DISCONNECTED and not session.ms.is_connected
            # whatever holds the device now is a session the fleet still owns
            owned = any(s.ms.is_connected for s in fleet.sessions.values())
            assert dev.connected == owned

    asyncio.run(main())


def test_stop_gather_cancels_an_in_flight_connect(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1, connect_delay=0.2)
        dev = sim.devices[0]
        dev.press_button()
        async with fleet:
            fleet.start_gather()
            await _until(lambda: fleet.connecting == 1)
            await fleet.stop_gather()
            assert fleet.connecting == 0 and not fleet.gathering
            await asyncio.sleep(0.3)  # long enough for an orphaned connect to land
            assert not dev.connected and not fleet.sessions

    asyncio.run(main())


def test_stop_gather_can_let_in_flight_connects_finish(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1, connect_delay=0.2)
        dev = sim.devices[0]
        dev.press_button()
        async with fleet:
            fleet.start_gather()
            await _until(lambda: fleet.connecting == 1)
            await fleet.stop_gather(finish_pending=True)
            assert fleet.connecting == 0 and not fleet.gathering
            assert list(fleet.sessions) == [_id(dev)] and dev.connected
            assert len(_of(events, SensorGathered)) == 1

    asyncio.run(main())


def test_release_all_fails_a_connect_still_in_flight(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1, connect_delay=0.2)
        dev = sim.devices[0]
        dev.press_button()
        task = asyncio.create_task(fleet.connect(dev.ble_device))
        await asyncio.sleep(0.05)  # connecting, not yet in fleet.sessions
        await fleet.aclose()
        with pytest.raises(MS605ConnectionError):
            await task
        await asyncio.sleep(0.05)
        assert not dev.connected and not fleet.sessions
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(main())


def test_registry_save_failure_keeps_the_link(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1)
        dev = sim.devices[0]
        fleet.registry.add_site("lab-a", "Lab A")
        fleet.registry.add_sensor(_id(dev), "lab-a", "Sensor 1")

        def full_disk(*_a):
            raise StorageError("disk full")

        fleet.storage.write_json_atomic = full_disk  # the last_seen/battery refresh cannot be saved
        async with fleet:
            dev.press_button()
            session = await fleet.connect(dev.ble_device)
            assert session.state is LinkState.CONNECTED and fleet.sessions[_id(dev)] is session
            gathered = _of(events, SensorGathered)[-1]
            assert (gathered.known, gathered.alias, gathered.resolved_pending) == (True, "Sensor 1", False)

    asyncio.run(main())


# -- batch calibration --------------------------------------------------------------


def test_preflight_warns_where_presence_is_detected(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 3)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            sim.devices[1].signal = [(500, 500)] * 7  # someone in the room
            sim.devices[2].drop_link()
            await _until(lambda: fleet.sessions[ids[2]].state is LinkState.LOST)
            snaps = await fleet.preflight(ids, window_s=0.05)
            assert list(snaps) == ids
            assert snaps[ids[0]].samples >= 2 and snaps[ids[0]].occupied is False
            assert snaps[ids[1]].presence is True and snaps[ids[1]].occupied is True
            lost = snaps[ids[2]]
            assert (lost.samples, lost.occupied, lost.error) == (0, None, "not connected")
            assert lost.device_id == ids[2]

    asyncio.run(main())


def test_batch_now_calibrates_every_device(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 3)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, timeout=TIMEOUT)
            assert batch.state is BatchState.WAITING and batch.jobs == {}
            results = await batch.wait()
            assert batch.state is BatchState.DONE and set(batch.jobs) == set(ids)
            assert list(results) == ids
            assert all(r.state is S.SUCCEEDED and r.history_saved for r in results.values())
            assert all(r.after == tuple(DEFAULT_LEARNED_THRESHOLDS) for r in results.values())
            changes = _of(events, BatchChanged)
            assert [e.state for e in changes] == [BatchState.WAITING, BatchState.RUNNING, BatchState.DONE]
            assert all(e.batch_id == batch.batch_id and e.device_ids == tuple(ids) for e in changes)
            lines = fleet.storage.history_path.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 3
            assert batch.retry_ids() == []

    asyncio.run(main())


def test_batch_after_n_seconds_holds_the_links_until_it_fires(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 2)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, start=0.5, timeout=TIMEOUT)
            assert abs(batch.fire_at - (time.time() + 0.5)) < 0.1
            await asyncio.sleep(0.4)  # longer than the 0.3 s idle drop
            assert batch.state is BatchState.WAITING
            assert all(dev.connected and not dev.calibrating for dev in sim.devices)
            assert not any(_writes(dev, TAG_DETECT_MODE) for dev in sim.devices)
            results = await batch.wait()
            assert all(r.state is S.SUCCEEDED for r in results.values())
            running = next(e for e in _of(events, BatchChanged) if e.state is BatchState.RUNNING)
            assert running.at >= batch.fire_at

    asyncio.run(main())


def test_batch_at_a_datetime_and_bad_arguments(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            with pytest.raises(ValueError):
                fleet.calibrate([])
            with pytest.raises(KeyError):
                fleet.calibrate(["0" * 16])
            with pytest.raises(ValueError):
                fleet.calibrate(ids, start=datetime.now() - timedelta(seconds=5))
            with pytest.raises(ValueError):
                fleet.calibrate(ids, start=-1.0)
            assert not _of(events, BatchChanged)

            at = datetime.now() + timedelta(seconds=0.3)  # naive: local time
            batch = fleet.calibrate(ids, start=at, timeout=TIMEOUT)
            assert batch.fire_at == at.timestamp()
            results = await batch.wait()
            assert results[ids[0]].state is S.SUCCEEDED
            running = next(e for e in _of(events, BatchChanged) if e.state is BatchState.RUNNING)
            assert running.at >= at.timestamp()

    asyncio.run(main())


def test_scheduled_batch_follows_the_wall_clock(tmp_path, monkeypatch):
    """A fast clock: an hour-ahead batch fires as soon as the wall clock gets
    there (here it jumps, as after a laptop sleeps), not after an hour of sleeping."""
    offset = [0.0]
    real_time = time.time
    monkeypatch.setattr(fleet_mod, "_FIRE_CHECK_S", 0.01)
    monkeypatch.setattr(fleet_mod, "time", SimpleNamespace(time=lambda: real_time() + offset[0]))

    async def main():
        sim, fleet, _ = _make(tmp_path, 2)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, start=datetime.now() + timedelta(hours=1), timeout=TIMEOUT)
            await asyncio.sleep(0.1)
            assert batch.state is BatchState.WAITING
            offset[0] = 3600.0
            results = await asyncio.wait_for(batch.wait(), 2.0)
            assert all(r.state is S.SUCCEEDED for r in results.values())

    asyncio.run(main())


def test_cancel_while_waiting_needs_no_device_io(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 2)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, start=60.0, timeout=TIMEOUT)
            await asyncio.sleep(0.05)
            await batch.cancel()
            assert batch.state is BatchState.CANCELLED and batch.jobs == {}
            results = await batch.wait()
            assert all(r.state is S.CANCELLED and not r.started for r in results.values())
            assert [e.state for e in _of(events, BatchChanged)] == [BatchState.WAITING, BatchState.CANCELLED]
            assert not any(_writes(dev, TAG_DETECT_MODE) for dev in sim.devices)
            assert all(s.state is LinkState.CONNECTED for s in fleet.sessions.values())
            await batch.cancel()  # already over: nothing happens
            assert len(_of(events, BatchChanged)) == 2

    asyncio.run(main())


def test_cancel_while_running_drops_every_link(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 2, calibration_secs=100)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, timeout=TIMEOUT)
            await _until(lambda: len(batch.jobs) == 2 and all(j.state is S.LEARNING for j in batch.jobs.values()))
            await batch.cancel()
            assert batch.state is BatchState.CANCELLED
            results = await batch.wait()
            assert all(r.state is S.CANCELLED and r.started for r in results.values())
            assert all(fleet.sessions[i].state is LinkState.DISCONNECTED for i in ids)
            assert all(dev.thresholds == MEDIUM and not dev.calibrating for dev in sim.devices)
            assert _of(events, BatchChanged)[-1].state is BatchState.CANCELLED

    asyncio.run(main())


def test_one_of_seven_dropping_mid_run_spares_the_others_and_only_it_is_retried(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 7, calibration_secs=30)
        dropped = sim.devices[3]
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, timeout=TIMEOUT)
            await _until(lambda: ids[3] in batch.jobs and batch.jobs[ids[3]].state is S.LEARNING)
            dropped.drop_link()
            results = await batch.wait()
            assert results[ids[3]].state is S.LOST and results[ids[3]].started
            assert all(results[i].state is S.SUCCEEDED for i in ids if i != ids[3])
            assert batch.retry_ids() == [ids[3]]

            # the person presses that sensor's button again; gathering brings it back
            fleet.start_gather()
            dropped.press_button()
            await _until(lambda: fleet.sessions[ids[3]].state is LinkState.CONNECTED)
            await fleet.stop_gather()
            retry = fleet.calibrate(batch.retry_ids(), timeout=TIMEOUT)
            assert retry.device_ids == (ids[3],)
            assert (await retry.wait())[ids[3]].state is S.SUCCEEDED
            starts = [_writes(dev, TAG_DETECT_MODE).count(bytes([DetectMode.SPACE_LEARNING])) for dev in sim.devices]
            assert starts == [1, 1, 1, 2, 1, 1, 1]

    asyncio.run(main())


def test_device_in_two_batches_and_a_released_device(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 2)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            first = fleet.calibrate([ids[0]], timeout=TIMEOUT)
            second = fleet.calibrate([ids[0]], timeout=TIMEOUT)
            later = fleet.calibrate([ids[1]], start=0.1, timeout=TIMEOUT)
            await fleet.release([ids[1]])  # gone before the batch fires
            one, two, gone = await first.wait(), await second.wait(), await later.wait()
            assert one[ids[0]].state is S.SUCCEEDED
            assert two[ids[0]].state is S.FAILED and two[ids[0]].error == "busy: calibration"
            assert gone[ids[1]].state is S.LOST and not gone[ids[1]].started
            assert gone[ids[1]].detail == "세션 없음" and later.jobs == {}
            assert later.retry_ids() == [ids[1]] and second.retry_ids() == [ids[0]]

    asyncio.run(main())


# -- drafts ---------------------------------------------------------------------------


def test_a_sensor_regathered_after_release_is_not_calibrated_by_an_older_batch(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1)
        dev = sim.devices[0]
        async with fleet:
            ids = await _gather_all(sim, fleet)
            batch = fleet.calibrate(ids, start=0.1, timeout=TIMEOUT)
            await fleet.release(ids)
            await fleet.connect(dev.ble_device)  # the same sensor, gathered again for something else
            result = (await batch.wait())[ids[0]]
            assert (result.state, result.started, result.detail) == (S.LOST, False, "세션 없음")
            assert not _writes(dev, TAG_DETECT_MODE) and fleet.sessions[ids[0]].busy is None

    asyncio.run(main())


def test_a_job_raising_unexpectedly_spares_the_other_jobs(tmp_path, monkeypatch):
    async def main():
        sim, fleet, _ = _make(tmp_path, 3)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            real_job = fleet_mod.CalibrationJob

            class Job(real_job):
                async def run(self):
                    if self.session.device_id == ids[1]:
                        raise RuntimeError("boom")  # outside run()'s contract
                    return await super().run()

            monkeypatch.setattr(fleet_mod, "CalibrationJob", Job)
            results = await fleet.calibrate(ids, timeout=TIMEOUT).wait()
            assert [results[i].state for i in ids] == [S.SUCCEEDED, S.FAILED, S.SUCCEEDED]
            assert "boom" in results[ids[1]].error

    asyncio.run(main())


def test_bulk_relative_and_per_sensor_absolute_thresholds(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 3)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            relative = ThresholdChange(relative=True, trigger=[5] * 7, maintain=[None] * 6 + [-2])
            absolute = ThresholdChange(relative=False, trigger=[100] * 7, maintain=[50] * 7)
            draft = Draft(
                list(ids),
                bulk=SensorChanges(sensitivity=Sensitivity.HIGH, zone_thresholds=relative),
                per_sensor={ids[2]: SensorChanges(zone_thresholds=absolute)},  # replaces that section only
            )
            results = await fleet.apply(draft)
            assert list(results) == ids
            assert all(r.status is ApplyStatus.OK and r.error is None for r in results.values())
            expected = [(t + 5, m) for t, m in MEDIUM[:6]] + [(MEDIUM[6][0] + 5, MEDIUM[6][1] - 2)]
            d1, d2, d3 = sim.devices
            assert d1.thresholds == d2.thresholds == expected
            assert d3.thresholds == [(100, 50)] * 7
            assert all(dev.sensitivity == Sensitivity.HIGH for dev in sim.devices)

            r = results[ids[0]]
            assert (r.reason, r.applied, r.skipped, r.mismatched) == (
                "apply",
                ("sensitivity", "zone_thresholds"),
                (),
                (),
            )
            snap = fleet.storage.load_snapshot(ids[0], r.snapshot)
            assert snap.reason == "apply" and snap.sections == ("sensitivity", "zone_thresholds")
            assert snap.profile.zone_thresholds == MEDIUM and snap.profile.sensitivity == Sensitivity.MEDIUM
            assert _of(events, ApplyResult) == list(results.values())

    asyncio.run(main())


def test_draft_is_validated_before_any_device_is_touched(tmp_path):
    def thresholds(trigger, relative=False):
        return SensorChanges(zone_thresholds=ThresholdChange(relative=relative, trigger=trigger, maintain=[None] * 7))

    async def main():
        sim, fleet, events = _make(tmp_path, 2)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            bad = [
                Draft(list(ids), bulk=thresholds([70_000] * 7)),  # absolute out of range
                Draft(list(ids), bulk=thresholds([1] * 6, relative=True)),  # 6 zones
                Draft(list(ids), bulk=thresholds([True] + [None] * 6)),  # bool is not a number
                Draft(list(ids), per_sensor={ids[1]: SensorChanges(sensitivity=9)}),
                Draft([], bulk=thresholds([1] * 7)),  # no targets
            ]
            for draft in bad:
                with pytest.raises(ProfileError):
                    await fleet.apply(draft)
            with pytest.raises(KeyError):
                await fleet.apply(Draft([ids[0], "0" * 16], bulk=thresholds([1] * 7)))
            assert not _of(events, ApplyResult)
            assert not any(_writes(dev, TAG_ZONE_THRESHOLDS) for dev in sim.devices)
            assert all(fleet.storage.list_snapshots(i) == [] for i in ids)

    asyncio.run(main())


def test_out_of_range_result_or_a_lost_link_fails_only_that_sensor(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 3)
        d1, d2, d3 = sim.devices
        async with fleet:
            ids = await _gather_all(sim, fleet)
            high = [(65_530, 30)] * 7
            d2.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(high)
            d3.drop_link()
            await _until(lambda: fleet.sessions[ids[2]].state is LinkState.LOST)
            change = ThresholdChange(relative=True, trigger=[10] * 7, maintain=[None] * 7)
            results = await fleet.apply(Draft(list(ids), bulk=SensorChanges(zone_thresholds=change)))
            assert results[ids[0]].status is ApplyStatus.OK
            assert d1.thresholds == [(t + 10, m) for t, m in MEDIUM]
            over = results[ids[1]]
            assert over.status is ApplyStatus.FAILED and "outside 0..65535" in over.error
            assert over.snapshot is None and over.applied == ()
            assert d2.thresholds == high and not _writes(d2, TAG_ZONE_THRESHOLDS)  # never clamped
            lost = results[ids[2]]
            assert (lost.status, lost.error, lost.snapshot) == (ApplyStatus.FAILED, "not connected", None)
            assert fleet.storage.list_snapshots(ids[1]) == [] and fleet.storage.list_snapshots(ids[2]) == []

    asyncio.run(main())


def test_busy_session_fails_the_apply_without_io(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            session = fleet.sessions[ids[0]]
            change = SensorChanges(zone_thresholds=ThresholdChange(False, [90] * 7, [40] * 7))
            async with session.operation("calibration"):
                result = await apply_changes(session, change, fleet.storage)
            assert (result.status, result.error, result.snapshot) == (ApplyStatus.FAILED, "busy: calibration", None)
            assert not _writes(sim.devices[0], TAG_ZONE_THRESHOLDS)

    asyncio.run(main())


def test_apply_to_an_unidentified_session_fails_clearly_without_io(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1)
        dev = sim.devices[0]
        dev.press_button()
        session = DeviceSession(dev.ble_device, fleet.bus, client_factory=sim.client_factory)
        await session.connect()  # no read_info(): no device id to keep a snapshot under
        change = SensorChanges(zone_thresholds=ThresholdChange(False, [90] * 7, [40] * 7))
        result = await apply_changes(session, change, fleet.storage)
        assert result.status is ApplyStatus.FAILED and "not identified" in result.error
        assert result.snapshot is None and not _writes(dev, TAG_ZONE_THRESHOLDS)
        await session.close()

    asyncio.run(main())


def test_verify_polls_through_the_apply_delay(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1, apply_delay=30)  # a tag51 write shows 0.3 s later
        dev = sim.devices[0]
        async with fleet:
            ids = await _gather_all(sim, fleet)
            session = fleet.sessions[ids[0]]
            first = SensorChanges(zone_thresholds=ThresholdChange(False, [90] * 7, [40] * 7))
            result = await apply_changes(session, first, fleet.storage)
            assert result.status is ApplyStatus.OK and result.mismatched == ()

            # a single early read-back would call it a failure: that is what polling avoids
            second = SensorChanges(zone_thresholds=ThresholdChange(False, [91] * 7, [None] * 7))
            result = await apply_changes(session, second, fleet.storage, verify_timeout=0.05)
            assert result.status is ApplyStatus.PARTIAL and result.mismatched == ("zone_thresholds",)
            await _until(lambda: dev.thresholds == [(91, 40)] * 7)  # it did land, only later

            async with session.operation("read") as ms:
                target = ConfigProfile(zone_thresholds=[(91, 40)] * 7)
                actual, mismatched = await poll_verify(ms, target, ["zone_thresholds"], timeout=0.1)
            assert mismatched == [] and actual.zone_thresholds == [(91, 40)] * 7

    asyncio.run(main())


def test_partial_failure_is_reported_per_device_and_rollback_restores_the_snapshot(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 3)
        d1, d2, d3 = sim.devices
        async with fleet:
            ids = await _gather_all(sim, fleet)
            d2.inject_status(5)  # the device refuses the next write
            change = ThresholdChange(relative=False, trigger=[90] * 7, maintain=[40] * 7)
            results = await fleet.apply(Draft(list(ids), bulk=SensorChanges(zone_thresholds=change)))
            assert [results[i].status for i in ids] == [ApplyStatus.OK, ApplyStatus.FAILED, ApplyStatus.OK]
            failed = results[ids[1]]
            assert "status 5" in failed.error and failed.applied == ()
            assert failed.mismatched == ("zone_thresholds",) and failed.snapshot is not None
            assert d1.thresholds == d3.thresholds == [(90, 40)] * 7 and d2.thresholds == MEDIUM

            undo = await fleet.rollback(ids[0], results[ids[0]].snapshot)
            assert (undo.status, undo.reason, undo.applied) == (ApplyStatus.OK, "rollback", ("zone_thresholds",))
            assert d1.thresholds == MEDIUM
            snaps = fleet.storage.list_snapshots(ids[0])
            assert [s.reason for s in snaps] == ["rollback", "apply"] and snaps[0].name == undo.snapshot

            redo = await fleet.rollback(ids[0], undo.snapshot)  # a rollback can itself be undone
            assert redo.status is ApplyStatus.OK and d1.thresholds == [(90, 40)] * 7
            with pytest.raises(StorageError):
                await fleet.rollback(ids[0], "20000101T000000000000Z")

    asyncio.run(main())


def test_clone_is_an_absolute_draft(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 3)
        src = sim.devices[0]
        custom = [(80 + z, 20 + z) for z in range(7)]
        src.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(custom)
        src.tags[TAG_ZONE_ENABLE] = bytes([0x3F])
        src.tags[TAG_DETECT_MODE] = bytes([DetectMode.SPACE_LEARNING])  # never cloned: it would start learning
        async with fleet:
            ids = await _gather_all(sim, fleet)
            async with fleet.sessions[ids[0]].operation("read") as ms:
                profile = ConfigProfile.from_config(await ms.read_config())
            changes = SensorChanges.from_profile(profile, PROFILE_SECTION_KEYS)
            assert changes.zone_thresholds == ThresholdChange(
                relative=False, trigger=[t for t, _ in custom], maintain=[m for _, m in custom]
            )
            with pytest.raises(ProfileError):
                SensorChanges.from_profile(profile, ["zone_distances"])

            results = await fleet.apply(Draft(ids[1:], bulk=changes))
            for device_id, dev in zip(ids[1:], sim.devices[1:], strict=True):
                result = results[device_id]
                assert result.status is ApplyStatus.OK and result.skipped == ("detect_mode",)
                assert "zone_thresholds" in result.applied and "zone_enable" in result.applied
                assert dev.thresholds == custom and dev.tags[TAG_ZONE_ENABLE] == bytes([0x3F])
                assert dev.detect_mode == DetectMode.RADAR_WITH_PIR and not dev.calibrating

    asyncio.run(main())


def test_pair_sections_given_as_json_lists_verify_ok(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            timing = [[5, 60], [6, 61], [7, 62]]  # as decoded from a JSON draft
            result = await apply_changes(
                fleet.sessions[ids[0]], SensorChanges(subsensor_timing=timing), fleet.storage, verify_timeout=0.05
            )
            assert (result.status, result.applied, result.mismatched) == (ApplyStatus.OK, ("subsensor_timing",), ())

    asyncio.run(main())


def test_a_write_that_cannot_be_read_back_is_unverified(tmp_path):
    async def main():
        sim, fleet, _ = _make(tmp_path, 1)
        async with fleet:
            ids = await _gather_all(sim, fleet)
            session = fleet.sessions[ids[0]]
            real_read = session.ms.read_config
            reads = 0

            async def read_config():
                nonlocal reads
                reads += 1
                if reads > 1:  # the verify read-back, after the write
                    raise MS605Error("synthetic read-back failure")
                return await real_read()

            session.ms.read_config = read_config
            change = SensorChanges(zone_thresholds=ThresholdChange(False, [90] * 7, [40] * 7))
            result = await apply_changes(session, change, fleet.storage)
            assert (result.status, result.applied) == (ApplyStatus.UNVERIFIED, ("zone_thresholds",))
            assert result.error == "synthetic read-back failure" and result.snapshot is not None
            assert sim.devices[0].thresholds == [(90, 40)] * 7

    asyncio.run(main())


def test_aclose_cancels_a_waiting_batch_and_results_carry_the_session_address(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 2)
        ids = await _gather_all(sim, fleet)  # neither sensor is in the registry
        addresses = {i: fleet.sessions[i].address for i in ids}
        batch = fleet.calibrate(ids, start=3600.0, timeout=TIMEOUT)
        await fleet.aclose()
        assert batch.state is BatchState.CANCELLED and batch._task.done()
        assert {i: r.address for i, r in batch.results.items()} == addresses
        assert all(r.state is S.CANCELLED for r in batch.results.values())
        assert fleet._batches == set() and fleet.sessions == {}
        assert not any(_writes(dev, TAG_DETECT_MODE) for dev in sim.devices)

    asyncio.run(main())


# -- M3: a batch fired right after a re-gather (docs/CORE_API.md 14) ------------------------


def test_batch_fired_inside_the_regather_identify_lock_waits_for_it(tmp_path):
    async def main():
        sim, fleet, events = _make(tmp_path, 1, connectable_window=20)  # a 0.2 s window
        dev = sim.devices[0]
        device_id = _id(dev)
        batches: list = []
        armed = [False]

        def fire_inside_the_lock(ev) -> None:
            # BusyChanged("identify") is emitted with the lock already held: a batch made
            # here is certain to fire inside the window (a CONNECTED trigger could race past it)
            if armed[0] and isinstance(ev, BusyChanged) and ev.busy == "identify":
                armed[0] = False
                batches.append(fleet.calibrate([device_id], timeout=TIMEOUT))

        fleet.bus.subscribe(fire_inside_the_lock)
        async with fleet:
            fleet.start_gather()
            dev.press_button()
            await _until(lambda: device_id in fleet.sessions)
            session = fleet.sessions[device_id]
            await asyncio.sleep(0.3)  # the window closes; keep-alive holds the link
            dev.drop_link()
            await _until(lambda: session.state is LinkState.LOST)

            dev.response_delay = 10.0  # 0.1 s of wall time: the identify read holds the lock that long
            armed[0] = True
            dev.press_button()
            await _until(lambda: batches)
            (batch,) = batches
            results = await asyncio.wait_for(batch.wait(), 5)
            assert results[device_id].state is S.SUCCEEDED, results[device_id]  # was FAILED("busy: identify")
            assert results[device_id].started and results[device_id].after == tuple(DEFAULT_LEARNED_THRESHOLDS)
            assert fleet.sessions[device_id] is session

    asyncio.run(main())
