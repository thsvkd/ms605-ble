"""The interactive CLI's link keep-alive -- the ping loop that holds the MS605
link open through idle waits (menu / live monitor) so the device does not drop
us for an idle central->device direction. cli._shared.LiveLink used to own it;
ms605.session.DeviceSession does now, and the CLI's ensure() helper brings a
dropped link back. Driven through the simulator; every value is synthetic."""

from __future__ import annotations

import asyncio

from ms605.cli._shared import ensure
from ms605.events import EventBus, LinkState
from ms605.session import DeviceSession
from ms605.sim import SimFleet


def _session(*, keepalive_interval: float = 0.05, **sim_kw):
    sim_kw.setdefault("speed", 1)
    sim_kw.setdefault("connectable_window", None)
    fleet = SimFleet(1, **sim_kw)
    dev = fleet.devices[0]
    session = DeviceSession(
        dev.ble_device,
        EventBus(),
        scan=fleet.discover,
        client_factory=fleet.client_factory,
        keepalive_interval=keepalive_interval,
        scan_secs=0.01,
    )
    return dev, session


def _pings(dev) -> int:
    return sum(1 for f in dev.frames_in if all(t == 1 for t, _ in f.attributes))


def test_keepalive_pings_periodically_while_connected():
    async def run():
        dev, session = _session(idle_timeout=0.3)  # drops after 0.3 s without a central write
        await session.connect()
        await asyncio.sleep(0.5)
        result = _pings(dev), dev.connected, session.state
        await session.close()
        return result

    pings, connected, state = asyncio.run(run())
    assert pings >= 2 and connected and state is LinkState.CONNECTED


def test_keepalive_stops_once_the_link_is_lost_and_after_close():
    async def run():
        dev, session = _session(idle_timeout=None)
        calls: list[int] = []
        real_ping = session.ms.ping

        async def counted_ping(**kw):
            calls.append(1)  # counted before the write: a ping on a dead link still shows
            await real_ping(**kw)

        session.ms.ping = counted_ping
        await session.connect()
        await asyncio.sleep(0.12)  # a couple of pings on the live link
        live = len(calls)
        dev.drop_link()
        while session.state is not LinkState.LOST:
            await asyncio.sleep(0.005)
        lost_at = len(calls)
        await asyncio.sleep(0.15)  # three intervals while LOST
        lost_after = len(calls)
        dev.press_button()
        await session.reconnect_once()
        await session.close()
        closed_at = len(calls)
        await asyncio.sleep(0.15)  # released: no more pings
        return live, lost_at, lost_after, closed_at, len(calls), asyncio.all_tasks() == {asyncio.current_task()}

    live, lost_at, lost_after, closed_at, after, settled = asyncio.run(run())
    assert live >= 1
    assert lost_after == lost_at  # no ping while LOST
    assert after == closed_at and settled


def test_keepalive_survives_ping_failure_and_keeps_trying():
    # a ping the device answers with an error status proves the link is up:
    # the loop must keep probing instead of giving the link up.
    async def run():
        dev, session = _session(idle_timeout=None)
        await session.connect()
        dev.inject_status(5)
        await asyncio.sleep(0.3)
        result = _pings(dev), session.state
        await session.close()
        return result

    pings, state = asyncio.run(run())
    assert pings >= 2 and state is LinkState.CONNECTED


def test_stop_keepalive_is_idempotent_and_safe_without_start():
    async def run():
        dev, session = _session(idle_timeout=None)
        await session.close()  # never connected -- must not raise
        await session.connect()
        await session.close()
        await session.close()  # double close -- must not raise
        return session.state, dev.connected, asyncio.all_tasks() == {asyncio.current_task()}

    assert asyncio.run(run()) == (LinkState.DISCONNECTED, False, True)


def test_ensure_is_a_no_op_on_a_live_link_and_reconnects_a_lost_one(capsys):
    async def run():
        dev, session = _session(idle_timeout=None)
        await session.connect()
        await ensure(session)
        quiet = capsys.readouterr().out
        dev.drop_link()
        while session.state is not LinkState.LOST:
            await asyncio.sleep(0.005)
        dev.press_button()
        await ensure(session)
        result = quiet, session.state, dev.connected
        await session.close()
        return result

    quiet, state, connected = asyncio.run(run())
    assert quiet == ""
    assert state is LinkState.CONNECTED and connected
    out = capsys.readouterr().out
    assert "BLE 연결이 끊어져 있습니다" in out and "재연결 성공" in out
