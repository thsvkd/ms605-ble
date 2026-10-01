"""ms605.session.DeviceSession against the protocol-level simulator: link state
machine, keep-alive, operation lock, live-output refcount and push events.
Every value here is synthetic."""

from __future__ import annotations

import asyncio

import pytest

import ms605.session as session_mod
from ms605 import MS605ConnectionError, MS605DeviceError, MS605Error, MS605TimeoutError, SessionBusyError
from ms605.events import (
    BusyChanged,
    EventBus,
    FrameDropped,
    KeepAliveMissed,
    LinkState,
    LinkStateChanged,
    LiveRadar,
    PirChanged,
)
from ms605.protocol import TAG_LIVE_OUTPUT_ENABLE, TAG_LIVE_RADAR_OUTPUT, TAG_PIR_STATE
from ms605.session import DeviceSession
from ms605.sim import SimBLEDevice, SimFleet

pytestmark = pytest.mark.usefixtures("no_chunk_pacing")

SPEED = 100.0
KEEPALIVE = 15 / SPEED  # the real 15 s interval, scaled like the simulator's idle drop (30 s -> 0.3 s)


def _setup(**sim_kw):
    """One simulated device (button pressed) and an unconnected session on it."""
    sim_kw.setdefault("speed", SPEED)
    fleet = SimFleet(1, **sim_kw)
    dev = fleet.devices[0]
    dev.press_button()
    bus = EventBus()
    events: list = []
    bus.subscribe(events.append)
    session = DeviceSession(
        dev.ble_device, bus, scan=fleet.discover, client_factory=fleet.client_factory, keepalive_interval=KEEPALIVE
    )
    return dev, session, events


def _links(events) -> list[tuple[LinkState, LinkState]]:
    return [(e.previous, e.state) for e in events if isinstance(e, LinkStateChanged)]


def _of(events, kind) -> list:
    return [e for e in events if isinstance(e, kind)]


async def _until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


def _live_writes(dev) -> list[int]:
    return [v[0] for f in dev.frames_in for t, v in f.attributes if t == TAG_LIVE_OUTPUT_ENABLE]


def _bare_pings(dev) -> int:
    return sum(1 for f in dev.frames_in if all(t == 1 for t, _ in f.attributes))


# -- link state machine ----------------------------------------------------------


def test_connect_then_close_walks_the_state_table():
    async def main():
        dev, session, events = _setup()
        assert session.state is LinkState.DISCONNECTED
        await session.connect()
        assert session.state is LinkState.CONNECTED and dev.connected
        with pytest.raises(MS605Error):
            await session.connect()
        await session.close()
        await session.close()  # DISCONNECTED -> DISCONNECTED: no event
        assert not dev.connected
        assert _links(events) == [
            (LinkState.DISCONNECTED, LinkState.CONNECTING),
            (LinkState.CONNECTING, LinkState.CONNECTED),
            (LinkState.CONNECTED, LinkState.DISCONNECTED),
        ]

    asyncio.run(main())


def test_failed_connect_returns_to_previous_state_with_reason():
    async def main():
        dev, session, events = _setup(connectable_window=5)
        dev._window_until = None  # no button press: not connectable
        with pytest.raises(MS605ConnectionError):
            await session.connect()
        assert session.state is LinkState.DISCONNECTED
        last = _of(events, LinkStateChanged)[-1]
        assert (last.previous, last.state) == (LinkState.CONNECTING, LinkState.DISCONNECTED)
        assert last.reason

    asyncio.run(main())


def test_cancelled_connect_returns_to_previous_state():
    async def main():
        dev, session, events = _setup()

        def hanging_factory(*args, **kwargs):
            client = dev.client_factory(*args, **kwargs)

            async def never(**_kw):
                await asyncio.sleep(60)

            client.connect = never
            return client

        session = DeviceSession(dev.ble_device, session.bus, client_factory=hanging_factory)
        task = asyncio.ensure_future(session.connect())
        await _until(lambda: session.state is LinkState.CONNECTING)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.state is LinkState.DISCONNECTED
        assert _links(events)[-1] == (LinkState.CONNECTING, LinkState.DISCONNECTED)

    asyncio.run(main())


def _slow_connect_factory(dev, delay: float):
    def factory(*args, **kwargs):
        client = dev.client_factory(*args, **kwargs)
        real_connect = client.connect

        async def slow_connect(**kw):
            await asyncio.sleep(delay)
            return await real_connect(**kw)

        client.connect = slow_connect
        return client

    return factory


def test_close_while_connecting_drops_the_link_that_lands_afterwards():
    async def main():
        dev, session, events = _setup()
        session = DeviceSession(
            dev.ble_device, session.bus, client_factory=_slow_connect_factory(dev, 0.1), keepalive_interval=KEEPALIVE
        )
        task = asyncio.ensure_future(session.connect())
        await _until(lambda: session.state is LinkState.CONNECTING)
        await session.close()
        assert session.state is LinkState.DISCONNECTED
        with pytest.raises(MS605ConnectionError, match="closed while connecting"):
            await task
        assert session.state is LinkState.DISCONNECTED and not dev.connected
        assert asyncio.all_tasks() == {asyncio.current_task()}  # no keep-alive left behind
        assert _links(events)[-2:] == [
            (LinkState.DISCONNECTED, LinkState.CONNECTING),
            (LinkState.CONNECTING, LinkState.DISCONNECTED),
        ]

    asyncio.run(main())


def test_keepalive_holds_the_link_past_the_idle_drop():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        await asyncio.sleep(3 * 30 / SPEED)  # three idle windows
        assert session.state is LinkState.CONNECTED and dev.connected
        assert _bare_pings(dev) >= 4
        await session.close()

    asyncio.run(main())


def test_without_keepalive_the_idle_drop_makes_the_link_lost():
    async def main():
        dev, session, events = _setup()
        session.keepalive_interval = 100.0  # effectively no keep-alive
        await session.connect()
        await _until(lambda: session.state is LinkState.LOST)
        assert _links(events)[-1] == (LinkState.CONNECTED, LinkState.LOST)
        assert _of(events, LinkStateChanged)[-1].reason
        assert session._keepalive_task is None
        await session.close()
        assert _links(events)[-1] == (LinkState.LOST, LinkState.DISCONNECTED)

    asyncio.run(main())


def test_drop_link_makes_the_session_lost_and_stops_pinging():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        assert session._keepalive_task is None
        with pytest.raises(MS605ConnectionError):
            async with session.operation("read"):
                pass

    asyncio.run(main())


def test_lost_link_comes_back_through_a_new_button_press():
    async def main():
        dev, session, events = _setup(connectable_window=10)
        await session.connect()
        await asyncio.sleep(0.15)  # the 0.1 s window closes
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        with pytest.raises(MS605ConnectionError):
            await session.reconnect_once()
        assert session.state is LinkState.LOST  # a failed attempt returns to LOST
        dev.press_button()
        await session.reconnect_once()
        assert session.state is LinkState.CONNECTED
        assert _links(events)[-2:] == [
            (LinkState.LOST, LinkState.CONNECTING),
            (LinkState.CONNECTING, LinkState.CONNECTED),
        ]
        await session.close()

    asyncio.run(main())


def test_connect_with_a_new_handle_from_lost():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        dev.press_button()
        await session.connect(dev.ble_device)
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_reconnect_once_rescans_and_uses_the_advertised_handle():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        dev.press_button()
        fresh = SimBLEDevice(dev.address, "MRBL_SIM01B")  # CoreBluetooth may hand out a new handle
        scans: list[float] = []

        async def scan(secs: float):
            scans.append(secs)
            return [fresh]

        session._scan = scan
        await session.reconnect_once()
        assert scans and session.state is LinkState.CONNECTED
        assert session.ms._address_or_device is fresh and session.name == "MRBL_SIM01B"
        await session.close()

    asyncio.run(main())


def test_reconnect_returns_when_another_caller_reconnected_meanwhile(monkeypatch):
    monkeypatch.setattr(session_mod, "_RECONNECT_PAUSE_S", 0.05)

    async def main():
        dev, session, events = _setup(connectable_window=10)
        await session.connect()
        await asyncio.sleep(0.15)  # the 0.1 s window closes
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        task = asyncio.ensure_future(session.reconnect(timeout=2.0))
        await asyncio.sleep(0.02)  # its first attempt failed; it is pausing
        dev.press_button()
        await session.connect()  # e.g. the gather loop got there first
        await asyncio.wait_for(task, 1.0)  # returns instead of "cannot connect while connected"
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_reconnect_waits_until_the_button_is_pressed(monkeypatch):
    monkeypatch.setattr(session_mod, "_RECONNECT_PAUSE_S", 0.02)

    async def main():
        dev, session, events = _setup(connectable_window=10)
        await session.connect()
        await asyncio.sleep(0.15)
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        task = asyncio.ensure_future(session.reconnect(timeout=2.0))
        await asyncio.sleep(0.1)
        assert not task.done()  # still retrying: nobody pressed the button
        dev.press_button()
        await asyncio.wait_for(task, 1.0)
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_reconnect_times_out_and_stays_lost(monkeypatch):
    monkeypatch.setattr(session_mod, "_RECONNECT_PAUSE_S", 0.02)

    async def main():
        dev, session, events = _setup(connectable_window=10)
        await session.connect()
        await asyncio.sleep(0.15)
        dev.drop_link()
        await _until(lambda: session.state is LinkState.LOST)
        with pytest.raises(MS605TimeoutError):
            await session.reconnect(timeout=0.1)
        assert session.state is LinkState.LOST

    asyncio.run(main())


def test_cancelled_reconnect_returns_to_previous_state(monkeypatch):
    monkeypatch.setattr(session_mod, "_RECONNECT_PAUSE_S", 0.02)

    async def main():
        dev, session, events = _setup(connectable_window=10)
        await asyncio.sleep(0.15)  # never connected, window closed: DISCONNECTED
        task = asyncio.ensure_future(session.reconnect(timeout=2.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.state is LinkState.DISCONNECTED

    asyncio.run(main())


@pytest.mark.parametrize("start", [LinkState.LOST, LinkState.DISCONNECTED])
def test_close_stops_a_running_reconnect(monkeypatch, start):
    monkeypatch.setattr(session_mod, "_RECONNECT_PAUSE_S", 0.02)

    async def main():
        dev, session, events = _setup(connectable_window=10)
        await session.connect()
        await asyncio.sleep(0.15)  # the button window closes
        if start is LinkState.LOST:
            dev.drop_link()
        else:
            await session.close()
        await _until(lambda: session.state is start)
        task = asyncio.ensure_future(session.reconnect(timeout=2.0))
        await asyncio.sleep(0.05)  # retrying: nobody pressed the button yet
        await session.close()  # the app lets go of the sensor
        dev.press_button()
        with pytest.raises(MS605ConnectionError, match="session closed"):
            await asyncio.wait_for(task, 1.0)
        await asyncio.sleep(0.05)
        assert session.state is LinkState.DISCONNECTED and not dev.connected
        assert session._keepalive_task is None

    asyncio.run(main())


# -- keep-alive misses ---------------------------------------------------------------


def test_unanswered_ping_with_link_up_is_a_miss_not_a_loss():
    async def main():
        dev, session, events = _setup(idle_timeout=None)
        await session.connect()
        dev.drop_responses(1)  # the session waits at most one interval for the ACK
        await _until(lambda: _of(events, KeepAliveMissed))
        assert session.state is LinkState.CONNECTED
        pings = _bare_pings(dev)
        await _until(lambda: _bare_pings(dev) > pings)  # keeps pinging
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_ping_answered_with_error_status_is_a_miss():
    async def main():
        dev, session, events = _setup(idle_timeout=None)
        ping = session.ms.ping
        failures = [MS605DeviceError(5, 1)]

        async def flaky_ping(**kw):
            if failures:
                raise failures.pop()
            await ping(**kw)

        session.ms.ping = flaky_ping
        await session.connect()
        await _until(lambda: _of(events, KeepAliveMissed))
        assert "status 5" in _of(events, KeepAliveMissed)[0].error
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_failed_ping_with_the_link_gone_makes_the_session_lost():
    async def main():
        dev, session, events = _setup(idle_timeout=None)

        async def dead_ping(**_kw):
            raise MS605ConnectionError("synthetic write failure")

        await session.connect()
        session.ms.ping = dead_ping
        await _until(lambda: session.state is LinkState.LOST)
        assert _of(events, LinkStateChanged)[-1].reason == "synthetic write failure"
        assert session._keepalive_task is None
        assert not _of(events, KeepAliveMissed)
        await session.close()
        assert session.state is LinkState.DISCONNECTED

    asyncio.run(main())


# -- operation lock ------------------------------------------------------------------


def test_operation_lock_fails_immediately_and_always_releases():
    async def main():
        dev, session, events = _setup()
        with pytest.raises(MS605ConnectionError):
            async with session.operation("read"):
                pass
        assert session.busy is None
        await session.connect()
        async with session.operation("apply") as ms:
            assert ms is session.ms
            assert session.busy == "apply"
            with pytest.raises(SessionBusyError) as info:
                async with session.operation("read"):
                    pass
            assert info.value.reason == "apply"
            await ms.read_config()
        with pytest.raises(RuntimeError):
            async with session.operation("read"):
                raise RuntimeError("synthetic")
        assert session.busy is None
        assert [e.busy for e in _of(events, BusyChanged)] == ["apply", None, "read", None]
        await session.close()

    asyncio.run(main())


def test_operation_lock_released_on_cancellation():
    async def main():
        dev, session, events = _setup()
        await session.connect()

        async def hold():
            async with session.operation("read"):
                await asyncio.sleep(60)

        task = asyncio.ensure_future(hold())
        await _until(lambda: session.busy == "read")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.busy is None
        await session.close()

    asyncio.run(main())


def test_read_info_identifies_the_device():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        info = await session.read_info()
        assert info.device_id == dev.tags[30].hex() == session.device_id
        assert (info.battery_pct, info.version, info.light_lux) == (87, (1, 0, 0), 120)
        assert [e.busy for e in _of(events, BusyChanged)] == ["identify", None]
        dev.tags[30] = b""
        with pytest.raises(MS605Error):
            await session.read_info()
        await session.close()

    asyncio.run(main())


# -- live output refcount -------------------------------------------------------------


def test_live_refcount_writes_tag54_only_on_first_and_last():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        await session.acquire_live()
        await session.acquire_live()
        assert _live_writes(dev) == [1]
        await _until(lambda: _of(events, LiveRadar))
        await session.release_live()
        assert _live_writes(dev) == [1]
        await session.release_live()
        assert _live_writes(dev) == [1, 0]
        await session.close()

    asyncio.run(main())


def test_live_output_is_rewritten_after_reconnect():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        await session.acquire_live()
        dev.drop_link()  # the device turns live output off on disconnect
        await _until(lambda: session.state is LinkState.LOST)
        assert dev.tags[TAG_LIVE_OUTPUT_ENABLE] == b"\x00"
        dev.press_button()
        await session.reconnect_once()
        assert dev.tags[TAG_LIVE_OUTPUT_ENABLE] == b"\x01"
        assert _live_writes(dev) == [1, 1]
        await session.release_live()
        await session.close()

    asyncio.run(main())


# -- pushes -> events ---------------------------------------------------------------------


def test_bad_tag55_pushes_never_break_the_stream():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        dev.corrupt_live("short")  # valid frame, undecodable value -> FrameDropped
        dev.corrupt_live("garbled")  # unparsable frame: the driver drops it before the session
        await session.acquire_live()
        await _until(lambda: len(_of(events, LiveRadar)) >= 2)
        dropped = _of(events, FrameDropped)
        assert len(dropped) == 1 and dropped[0].tag == 55 and dropped[0].reason
        assert session.last_radar is _of(events, LiveRadar)[-1].snapshot
        assert session.state is LinkState.CONNECTED
        await session.release_live()
        await session.close()

    asyncio.run(main())


def test_undecodable_tag55_does_not_hide_the_rest_of_its_frame():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        dev._push([(TAG_LIVE_RADAR_OUTPUT, bytes(10)), (TAG_PIR_STATE, b"\x01")])
        await _until(lambda: _of(events, PirChanged))
        assert [(e.tag, bool(e.reason)) for e in _of(events, FrameDropped)] == [(55, True)]
        assert [e.detected for e in _of(events, PirChanged)] == [True]
        assert session.last_radar is None and session.last_pir is True
        await session.close()

    asyncio.run(main())


def test_pir_changed_only_on_change():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        for value in (b"\x00", b"\x00", b"\x01", b"\x01", b"\x00"):
            dev._push([(TAG_PIR_STATE, value)])
            await asyncio.sleep(0.01)
        assert [e.detected for e in _of(events, PirChanged)] == [False, True, False]
        assert session.last_pir is False
        await session.close()

    asyncio.run(main())


def test_pir_values_from_an_earlier_link_or_stream_are_not_current():
    async def main():
        dev, session, events = _setup()
        await session.connect()
        await session.acquire_live()
        dev._push([(TAG_PIR_STATE, b"\x01")])
        await _until(lambda: session.last_pir is True)
        await session.release_live()
        await session.acquire_live()  # a new stream: its first value is reported even if unchanged
        assert session.last_pir is None
        dev._push([(TAG_PIR_STATE, b"\x01")])
        await _until(lambda: len(_of(events, PirChanged)) == 2 and session.last_radar is not None)
        await session.release_live()
        await session.close()
        dev.press_button()
        await session.connect()  # a new link: nothing carried over
        assert session.last_pir is None and session.last_radar is None
        dev._push([(TAG_PIR_STATE, b"\x01")])
        await _until(lambda: len(_of(events, PirChanged)) == 3)
        assert [e.detected for e in _of(events, PirChanged)] == [True, True, True]
        await session.close()

    asyncio.run(main())
