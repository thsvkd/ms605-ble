"""Offline checks for LiveLink's background keep-alive -- the ping loop that
holds the MS605 link open through idle waits (menu / live monitor) so the
device does not drop us for an idle central->device direction. No BLE hardware:
a fake ms records ping() calls."""

from __future__ import annotations

import asyncio

from ms605.cli._shared import LiveLink


class _FakeMS:
    def __init__(self, *, connected: bool = True, fail: bool = False) -> None:
        self.is_connected = connected
        self.fail = fail
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1
        if self.fail:
            raise RuntimeError("link gone")


def _run_keepalive(fake: _FakeMS, *, seconds: float = 0.1) -> int:
    async def run() -> int:
        link = LiveLink(fake, device=None, scan_secs=1.0, connect_timeout=1.0, keepalive_interval=0.02)
        link.start_keepalive()
        await asyncio.sleep(seconds)
        await link.stop_keepalive()
        return fake.pings

    return asyncio.run(run())


def test_keepalive_pings_periodically_while_connected():
    assert _run_keepalive(_FakeMS()) >= 2


def test_keepalive_skips_while_disconnected():
    assert _run_keepalive(_FakeMS(connected=False)) == 0


def test_keepalive_survives_ping_failure_and_keeps_trying():
    # a failing ping (link dropped) must not kill the loop -- ensure() recovers
    # the link on the next real operation, keep-alive just keeps probing.
    assert _run_keepalive(_FakeMS(fail=True)) >= 2


def test_stop_keepalive_is_idempotent_and_safe_without_start():
    async def run() -> None:
        link = LiveLink(_FakeMS(), device=None, scan_secs=1.0, connect_timeout=1.0)
        await link.stop_keepalive()  # never started -- must not raise
        link.start_keepalive()
        await link.stop_keepalive()
        await link.stop_keepalive()  # double stop -- must not raise

    asyncio.run(run())
