from __future__ import annotations

import asyncio
import json
import time

import pytest

from ms605.driver import MS605
from ms605.errors import StorageError
from ms605.events import LinkState
from ms605.fleet import Fleet
from ms605.protocol import TAG_DEVICE_ID
from ms605.registry import Registry
from ms605.sim import SimFleet
from ms605.storage import Storage

pytestmark = pytest.mark.usefixtures("no_chunk_pacing")


def _id(device) -> str:
    return device.tags[TAG_DEVICE_ID].hex()


def _fleet(storage: Storage, sim: SimFleet, *, scan=None, client_factory=None) -> Fleet:
    return Fleet(
        Registry(storage, host="host-1"),
        storage,
        scan=scan or sim.discover,
        client_factory=client_factory or sim.client_factory,
        keepalive_interval=3600,
        gather_pause=0.01,
    )


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)


async def _remember_unregistered(storage: Storage, sim: SimFleet) -> str:
    fleet = _fleet(storage, sim)
    fleet.start_recovery(interval=0.01)
    await fleet.stop_recovery()
    device = sim.devices[0]
    device.press_button()
    await fleet.connect(device.ble_device)
    device_id = _id(device)
    assert device_id not in fleet.registry.sensors
    await fleet.aclose()
    return device_id


def test_restart_restores_unregistered_placeholder_and_reconnects(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        device_id = await _remember_unregistered(storage, sim)

        restarted = _fleet(storage, sim)
        restarted.start_recovery(interval=0.01)
        assert restarted.recovery_enabled
        restored = restarted.sessions[device_id]
        assert restored.state is LinkState.DISCONNECTED
        assert restored.info is not None and restored.info.device_id == device_id
        assert restored.address == sim.devices[0].address

        sim.devices[0].press_button()
        await _until(lambda: restarted.sessions[device_id].state is LinkState.CONNECTED)
        await restarted.aclose()

        again = _fleet(storage, sim)
        again.start_recovery(interval=0.01)
        await again.stop_recovery()
        assert device_id in again.sessions  # shutdown preserves the remembered sensor
        await again.aclose()

    asyncio.run(run())


def test_first_migration_seeds_registered_cache_but_release_prevents_reseeding(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        device = sim.devices[0]
        device_id = _id(device)
        registry = Registry(storage, host="host-1")
        registry.add_site("lab", "Lab")
        registry.add_sensor(device_id, "lab", "Sensor")
        registry.match(device_id, device.address)

        fleet = Fleet(registry, storage, scan=sim.discover, client_factory=sim.client_factory)
        fleet.start_recovery(interval=0.01)
        await fleet.stop_recovery()
        assert fleet.sessions[device_id].state is LinkState.DISCONNECTED
        await fleet.release([device_id])
        await fleet.aclose()

        data = storage.read_json(storage.root / "connections.json")
        assert data is not None and data["hosts"]["host-1"] == {}
        restarted = _fleet(storage, sim)
        restarted.start_recovery(interval=0.01)
        await restarted.stop_recovery()
        assert device_id not in restarted.sessions
        await restarted.aclose()

    asyncio.run(run())


def test_gather_handles_remembered_sensor_while_recovery_skips_scans(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(2, speed=100)
        device_id = await _remember_unregistered(storage, sim)
        calls: list[float] = []

        async def scan(timeout: float):
            calls.append(timeout)
            return await sim.discover(timeout)

        fleet = _fleet(storage, sim, scan=scan)
        fleet.start_recovery(interval=0.02)
        fleet.start_gather()
        sim.devices[0].press_button()
        sim.devices[1].press_button()
        await _until(lambda: fleet.sessions[device_id].state is LinkState.CONNECTED)
        await asyncio.sleep(0.04)
        assert 1.0 not in calls and 0.02 not in calls
        assert _id(sim.devices[1]) in fleet.sessions  # explicit gather may still add a new sensor
        await fleet.aclose()

    asyncio.run(run())


def test_recovery_ignores_unknown_and_busy_and_does_not_overlap_slow_connect(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(2, speed=100)
        device_id = await _remember_unregistered(storage, sim)
        scans: list[float] = []
        active = 0
        maximum = 0
        connects = 0

        async def scan(timeout: float):
            scans.append(timeout)
            return await sim.discover(timeout)

        def factory(*args, **kwargs):
            client = sim.client_factory(*args, **kwargs)
            real = client.connect

            async def slow_connect(**connect_kwargs):
                nonlocal active, maximum, connects
                active += 1
                connects += 1
                maximum = max(maximum, active)
                try:
                    await asyncio.sleep(0.08)
                    return await real(**connect_kwargs)
                finally:
                    active -= 1

            client.connect = slow_connect
            return client

        fleet = _fleet(storage, sim, scan=scan, client_factory=factory)
        fleet.start_recovery(interval=0.01)
        remembered = fleet.sessions[device_id]
        remembered.busy = "calibration"
        sim.press_all()
        await asyncio.sleep(0.04)
        assert scans == [] and _id(sim.devices[1]) not in fleet.sessions

        remembered.busy = None
        await _until(lambda: remembered.state is LinkState.CONNECTED)
        assert maximum == 1 and connects == 1
        await asyncio.sleep(0.03)
        assert _id(sim.devices[1]) not in fleet.sessions
        count = len(scans)
        await asyncio.sleep(0.03)
        assert len(scans) == count  # all remembered sensors connected: no scan
        await fleet.aclose()

    asyncio.run(run())


def test_recovery_scan_uses_real_one_second_cadence(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        await _remember_unregistered(storage, sim)
        calls: list[tuple[float, float]] = []

        async def empty_scan(timeout: float):
            calls.append((timeout, time.monotonic()))
            return []

        fleet = _fleet(storage, sim, scan=empty_scan)
        fleet.start_recovery()
        await _until(lambda: len(calls) >= 3, timeout=2.4)
        assert [timeout for timeout, _ in calls[:3]] == [1.0, 1.0, 1.0]
        deltas = [calls[index + 1][1] - calls[index][1] for index in range(2)]
        assert all(0.9 <= delta <= 1.2 for delta in deltas)
        await fleet.aclose()

    asyncio.run(run())


def test_recovery_matches_remembered_mac_after_host_address_changes(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        device = sim.devices[0]
        mac = "C4:E7:AE:12:34:56"
        monkeypatch.setattr(MS605, "_scan_rssi", {})
        monkeypatch.setattr(MS605, "_scan_mac", {})
        MS605._scan_rssi[device.address] = (-60, time.monotonic())
        MS605._scan_mac[device.address] = mac

        fleet = _fleet(storage, sim, client_factory=device.client_factory)
        fleet.start_recovery(interval=0.01)
        await fleet.stop_recovery()
        device.press_button()
        session = await fleet.connect(device.ble_device)
        device_id = mac.replace(":", "").lower()
        assert session.device_id == device_id
        await fleet.aclose()

        device.address = "new-host-address"
        MS605._scan_rssi[device.address] = (-60, time.monotonic())
        MS605._scan_mac[device.address] = mac
        restarted = _fleet(storage, sim, client_factory=device.client_factory)
        restarted.start_recovery(interval=0.01)
        device.press_button()
        await _until(lambda: restarted.sessions[device_id].state is LinkState.CONNECTED)
        assert restarted.sessions[device_id].address == "new-host-address"
        await restarted.aclose()

    asyncio.run(run())


def test_recovery_prefers_mac_when_host_reuses_another_sensors_address(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        device = sim.devices[0]
        old_b_address = device.address
        mac_a = "C4:E7:AE:12:34:56"
        mac_b = "C4:E7:AE:65:43:21"
        id_a = mac_a.replace(":", "").lower()
        id_b = mac_b.replace(":", "").lower()
        storage.write_json_atomic(
            storage.root / "connections.json",
            {
                "format": "ms605-connections",
                "version": 1,
                "hosts": {
                    "host-1": {
                        id_a: {"address": "old-a-address", "name": "A", "mac": mac_a},
                        id_b: {"address": old_b_address, "name": "B", "mac": mac_b},
                    }
                },
            },
        )
        monkeypatch.setattr(MS605, "_scan_rssi", {old_b_address: (-60, time.monotonic())})
        monkeypatch.setattr(MS605, "_scan_mac", {old_b_address: mac_a})

        fleet = _fleet(storage, sim, client_factory=device.client_factory)
        fleet.start_recovery(interval=0.01)
        handle = device.ble_device
        assert fleet._recovery_session(handle) is fleet.sessions[id_a]
        MS605._scan_mac[old_b_address] = "C4:E7:AE:00:00:01"
        assert fleet._recovery_session(handle) is None
        MS605._scan_mac.pop(old_b_address)
        assert fleet._recovery_session(handle) is fleet.sessions[id_b]
        MS605._scan_mac[old_b_address] = mac_a

        device.press_button()
        await _until(lambda: fleet.sessions[id_a].state is LinkState.CONNECTED)
        assert fleet.sessions[id_a].address == old_b_address
        await fleet.aclose()

        restarted = _fleet(storage, sim)
        restarted.start_recovery(interval=0.01)
        await restarted.stop_recovery()
        assert restarted.sessions[id_a].address == restarted.sessions[id_b].address == old_b_address
        await restarted.aclose()

    asyncio.run(run())


def test_manual_gather_waits_for_recovery_scan_and_discards_its_results(tmp_path) -> None:
    async def run() -> None:
        storage = Storage(tmp_path / "data")
        sim = SimFleet(1, speed=100)
        await _remember_unregistered(storage, sim)
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        calls = 0

        async def scan(_timeout: float):
            nonlocal active, maximum, calls
            calls += 1
            active += 1
            maximum = max(maximum, active)
            try:
                if calls == 1:
                    entered.set()
                    await release.wait()
                return await sim.discover(0)
            finally:
                active -= 1

        fleet = _fleet(storage, sim, scan=scan)
        fleet.start_recovery(interval=0.01)
        sim.devices[0].press_button()
        await entered.wait()
        fleet.start_gather()
        release.set()
        await _until(lambda: fleet.sessions[_id(sim.devices[0])].state is LinkState.CONNECTED)
        assert maximum == 1
        await fleet.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "data",
    [
        {"format": "wrong", "version": 1, "hosts": {}},
        {"format": "ms605-connections", "version": 1, "hosts": []},
        {
            "format": "ms605-connections",
            "version": 1,
            "hosts": {"host-1": {"not-hex": {"address": "a", "name": None, "mac": None}}},
        },
        {
            "format": "ms605-connections",
            "version": 1,
            "hosts": {"host-1": {"aabbccddeeff": {"address": "a", "name": None, "mac": "00:11:22:33:44:55"}}},
        },
    ],
)
def test_recovery_rejects_malformed_connection_files(tmp_path, data) -> None:
    storage = Storage(tmp_path / "data")
    storage.root.mkdir(parents=True)
    (storage.root / "connections.json").write_text(json.dumps(data), encoding="utf-8")
    fleet = _fleet(storage, SimFleet(0))
    with pytest.raises(StorageError, match="connections"):
        fleet.start_recovery()
