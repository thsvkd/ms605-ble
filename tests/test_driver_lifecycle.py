"""Connection-lifecycle and failure-path regressions for ms605.MS605, driven
against the protocol-level simulator (ms605.sim). No BLE hardware; every
address and value here is synthetic."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import time

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import ms605.driver as driver_mod
from ms605 import MS605, MS605ConnectionError, MS605DeviceError, MS605Error, MS605TimeoutError
from ms605.cli._shared import _fmt_device_choice
from ms605.errors import ProfileError
from ms605.events import EventBus, LinkState
from ms605.models import ConfigProfile, encode_segment_map
from ms605.protocol import (
    PUSH_TRIGGER_SRC,
    TAG_BATTERY,
    TAG_DETECT_MODE,
    TAG_DEVICE_ID,
    TAG_PIR_STATE,
    TAG_PRESENCE_HISTORY_PUSH,
    TAG_SPACE_LEARNING_RESULT,
    TAG_STATUS,
    TAG_ZONE_THRESHOLDS,
    Sensitivity,
    build_frame_raw,
    parse_frame,
)
from ms605.session import DeviceSession
from ms605.sim import SimAdvertisement, SimFleet, SimMS605

PAIRS = [(90, 41), (80, 41), (70, 41), (60, 41), (50, 41), (40, 31), (30, 21)]


def _run(coro):
    return asyncio.run(coro)


def _dev(**kw) -> SimMS605:
    # no button window / idle drop unless a test opts in
    kw.setdefault("connectable_window", None)
    kw.setdefault("idle_timeout", None)
    return SimMS605(**kw)


async def _connect(dev: SimMS605, **kw) -> MS605:
    kw.setdefault("inter_chunk_delay", 0)
    ms = MS605(dev.address, client_factory=dev.client_factory, **kw)
    await ms.connect()
    return ms


async def _until(pred, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.001)


# -- reconnect / disconnect fail waiters with MS605ConnectionError ------------


def test_reconnect_fails_in_flight_read_with_connection_error_not_cancelled():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        lost = []
        ms.on_disconnect = lambda: lost.append(True)
        dev.drop_responses(1)
        task = asyncio.create_task(ms.read_raw([TAG_BATTERY]))
        await _until(lambda: len(dev.frames_in) == 1)
        await ms.reconnect()
        with pytest.raises(MS605ConnectionError):
            await task
        assert ms._pending == {}
        await asyncio.sleep(0.01)  # the old link's disconnect callback has run by now
        assert lost == []  # a link we let go of is not a "link lost" event
        await ms.ping()  # and it did not fail the new session

    _run(main())


def test_reconnect_during_calibration_raises_connection_error_not_cancelled():
    async def main():
        dev = _dev(calibration_secs=60, live_interval=10)
        ms = await _connect(dev)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=5, keepalive_interval=10))
        await _until(lambda: dev.calibrating and not ms._pending)
        await ms.reconnect()
        with pytest.raises(MS605ConnectionError):
            await asyncio.wait_for(task, 1.0)

    _run(main())


def test_disconnect_fails_pending_and_resets_reassembler():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.drop_responses(1)
        task = asyncio.create_task(ms.read_raw([TAG_BATTERY], timeout=5))
        await _until(lambda: len(dev.frames_in) == 1)
        ms._on_notify(None, bytearray(b"\x55\xaa\x00\x00"))  # half a frame in the buffer
        await ms.disconnect()
        with pytest.raises(MS605ConnectionError):
            await asyncio.wait_for(task, 0.5)
        assert ms._pending == {}
        assert ms._reassembler._buffer == b""
        assert not ms.is_connected and not dev.connected

    _run(main())


def _session(dev: SimMS605, keepalive_interval: float) -> DeviceSession:
    # the CLI's keep-alive owner (it replaced cli._shared.LiveLink)
    return DeviceSession(
        dev.ble_device, EventBus(), client_factory=dev.client_factory, keepalive_interval=keepalive_interval
    )


def test_live_link_keepalive_survives_reconnect():
    async def main():
        dev = _dev()
        session = _session(dev, 0.05)  # each ping waits at most 0.05 s
        await session.connect()
        dev.drop_responses(1)
        await _until(lambda: len(dev.frames_in) >= 1)  # first ping is waiting for an ACK
        dev.drop_link()  # the link goes while that ping waits
        await _until(lambda: session.state is LinkState.LOST)
        await session.connect()  # back from LOST
        seen = len(dev.frames_in)
        await _until(lambda: len(dev.frames_in) >= seen + 2)  # pings keep flowing on the new link
        assert not session._keepalive_task.done()
        await session.close()

    _run(main())


# -- link loss is noticed immediately ------------------------------------------


def test_link_drop_mid_calibration_fails_fast_while_ping_awaits_ack():
    async def main():
        dev = _dev(calibration_secs=60, live_interval=10)
        ms = await _connect(dev)
        lost = []
        ms.on_disconnect = lambda: lost.append(ms.is_connected)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=30, keepalive_interval=0.02))
        await _until(lambda: dev.calibrating and not ms._pending)
        dev.drop_responses(10)
        seen = len(dev.frames_in)
        await _until(lambda: len(dev.frames_in) > seen and ms._pending)  # a ping awaits its ACK
        dev.drop_link()
        start = time.monotonic()
        with pytest.raises(MS605ConnectionError):
            await asyncio.wait_for(task, 1.0)
        assert time.monotonic() - start < 1.0  # not the 10 s response timeout
        assert lost == [False]
        assert ms._pending == {}
        assert not dev.calibrating  # the device abandons learning on disconnect

    _run(main())


# -- BLE errors surface as MS605ConnectionError --------------------------------


def test_write_failure_is_connection_error_and_leaks_no_pending():
    async def main():
        dev = _dev()
        ms = await _connect(dev)

        async def broken_write(*_args, **_kwargs):
            raise OSError("adapter went away")

        ms._client.write_gatt_char = broken_write
        with pytest.raises(MS605ConnectionError):
            await ms.ping()
        assert ms._pending == {}

    _run(main())


def test_ops_after_peer_drop_raise_connection_error():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.drop_link()
        with pytest.raises(MS605ConnectionError):
            await ms.ping()
        assert not ms.is_connected

    _run(main())


def test_start_notify_failure_disconnects_the_client():
    async def main():
        dev = _dev()

        def factory(*args, **kwargs):
            client = dev.client_factory(*args, **kwargs)

            async def broken_notify(*_a, **_k):
                raise BleakError("notify setup failed")

            client.start_notify = broken_notify
            return client

        ms = MS605(dev.address, client_factory=factory, inter_chunk_delay=0)
        with pytest.raises(MS605ConnectionError):
            await ms.connect()
        assert not dev.connected  # the single central slot is free again
        await (await _connect(dev)).ping()

    _run(main())


def test_cancelled_connect_disconnects_the_client():
    async def main():
        dev = _dev()

        def factory(*args, **kwargs):
            client = dev.client_factory(*args, **kwargs)

            async def hanging_notify(*_a, **_k):
                await asyncio.Event().wait()

            client.start_notify = hanging_notify
            return client

        ms = MS605(dev.address, client_factory=factory, inter_chunk_delay=0)
        task = asyncio.create_task(ms.connect())
        await _until(lambda: dev.connected)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not dev.connected

    _run(main())


def test_connect_while_connected_refuses_without_a_new_client():
    async def main():
        dev = _dev()
        made = []

        def factory(*args, **kwargs):
            made.append(dev.client_factory(*args, **kwargs))
            return made[-1]

        ms = MS605(dev.address, client_factory=factory, inter_chunk_delay=0)
        await ms.connect()
        with pytest.raises(MS605Error, match="already connected"):
            await ms.connect()
        assert len(made) == 1 and ms._client is made[0]
        await ms.ping()

    _run(main())


# -- response validation -------------------------------------------------------


def test_any_failed_status_in_a_multi_write_raises_device_error():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.inject_status(0, 5, 0)
        with pytest.raises(MS605DeviceError) as err:
            await ms.set_subsensor_config(
                enabled=[True, True, True],
                segment_map=encode_segment_map([[0], [1], [2]]),
                presence_absence_times=[(5, 30)] * 3,
            )
        assert err.value.status == 5

    _run(main())


def test_bad_crc_response_is_dropped():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.corrupt_next_crc()
        with pytest.raises(MS605TimeoutError):
            await ms.read_raw([TAG_BATTERY], timeout=0.1)
        assert (await ms.read_raw([TAG_BATTERY])).get(TAG_BATTERY) == dev.tags[TAG_BATTERY]

    _run(main())


def test_bad_crc_warning_does_not_log_the_frame_contents(caplog):
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.corrupt_next_crc()
        with caplog.at_level("WARNING", logger="ms605.driver"):
            with pytest.raises(MS605TimeoutError):
                await ms.read_raw([TAG_DEVICE_ID], timeout=0.1)
        return dev.tags[TAG_DEVICE_ID].hex()

    device_id_hex = _run(main())
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("bad CRC" in m for m in warnings)
    assert not any(device_id_hex in m for m in warnings)  # tag 30 id stays out of shareable logs


def test_read_via_push_or_response_total_wait_is_one_timeout():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        dev.response_delay = 0.3  # the read's answer eats most of the budget...
        dev._read = lambda _value: (TAG_STATUS, b"\x00")  # ...and lacks the tag, forcing the push wait
        start = time.monotonic()
        with pytest.raises(MS605TimeoutError):
            await ms._read_via_push_or_response(TAG_PRESENCE_HISTORY_PUSH, timeout=0.4)
        assert time.monotonic() - start < 0.6  # was read + a full second timeout (~0.7 s)
        assert ms._push_waiters.get(TAG_PRESENCE_HISTORY_PUSH) in (None, [])

    _run(main())


# -- push dispatch isolation ---------------------------------------------------


def test_throwing_push_handler_does_not_eat_response_or_other_handlers():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        seen = []

        def bad(_frame):
            raise RuntimeError("handler bug")

        ms.add_push_handler(bad)
        ms.add_push_handler(seen.append)
        dev.drop_responses(1)
        task = asyncio.create_task(ms.read_raw([TAG_BATTERY], timeout=1))
        await _until(lambda: len(dev.frames_in) == 1)
        push = build_frame_raw([(TAG_PIR_STATE, b"\x01")], 0, trigger_src=PUSH_TRIGGER_SRC)
        resp = build_frame_raw([(TAG_BATTERY, b"\x2a")], dev.frames_in[-1].msg_id)
        ms._on_notify(None, bytearray(push + resp))  # one notification, push first
        assert (await task).get(TAG_BATTERY) == b"\x2a"
        assert [f.get(TAG_PIR_STATE) for f in seen] == [b"\x01"]

    _run(main())


def test_handler_removing_itself_does_not_skip_the_next_one():
    ms = MS605("02:00:00:00:00:01")
    calls = []

    def once(_frame):
        calls.append("once")
        ms.remove_push_handler(once)

    ms.add_push_handler(once)
    ms.add_push_handler(lambda _frame: calls.append("other"))
    ms._on_notify(None, bytearray(build_frame_raw([(TAG_PIR_STATE, b"\x01")], 0, trigger_src=PUSH_TRIGGER_SRC)))
    assert calls == ["once", "other"]


# -- cancellation --------------------------------------------------------------


def test_cancel_mid_write_finishes_the_frame_and_leaks_no_pending():
    async def main():
        dev = _dev()
        ms = await _connect(dev, inter_chunk_delay=0.01)
        client = ms._client
        task = asyncio.create_task(ms.set_zone_thresholds(PAIRS))
        await _until(lambda: len(client.written) == 1)  # first of 3 chunks is out
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: dev.frames_in and dev.frames_in[-1].get(TAG_ZONE_THRESHOLDS) is not None)
        frame = parse_frame(b"".join(client.written))  # the chunks form one whole frame
        assert frame.crc_ok and len(client.written) == 3
        assert ms._pending == {}
        await ms.ping()  # the wire is still in sync

    _run(main())


def _wedge_writes(client) -> None:
    """Make every further write on `client` hang forever (a stuck backend write)."""

    async def forever(*_a, **_kw):
        await asyncio.Event().wait()

    client.write_gatt_char = forever


def test_stalled_write_times_out_and_abandons_the_link():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        _wedge_writes(ms._client)
        t0 = time.monotonic()
        with pytest.raises(MS605TimeoutError):
            await ms.ping(timeout=0.2)
        assert time.monotonic() - t0 < 1.0
        assert not ms.is_connected and not dev.connected  # a half-written frame: the link is dropped
        assert ms._pending == {}

    _run(main())


def test_cancel_during_a_stalled_write_is_still_bounded():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        _wedge_writes(ms._client)
        task = asyncio.create_task(ms.ping(timeout=0.3))
        await asyncio.sleep(0.05)
        task.cancel()
        t0 = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        assert time.monotonic() - t0 < 1.0
        assert not ms.is_connected

    _run(main())


def test_disconnect_ends_a_cancelled_write_that_is_stuck():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        _wedge_writes(ms._client)
        task = asyncio.create_task(ms.ping())  # 10 s default budget
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # still protecting the frame on a live link
        await ms.disconnect()  # the link is gone: the partial frame no longer matters
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    _run(main())


def test_disconnect_fails_an_uncancelled_stuck_write_with_connection_error():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        _wedge_writes(ms._client)
        task = asyncio.create_task(ms.ping())
        await asyncio.sleep(0.05)
        await ms.disconnect()
        with pytest.raises(MS605ConnectionError):
            await asyncio.wait_for(task, timeout=0.5)

    _run(main())


# -- profile validation ----------------------------------------------------------


def test_invalid_profile_writes_nothing():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        before = len(dev.frames_in)
        profile = ConfigProfile(sensitivity=Sensitivity.HIGH, zone_thresholds=PAIRS[:6])  # 6 of 7 zones
        with pytest.raises(ProfileError):
            await ms.apply_profile(profile)
        assert len(dev.frames_in) == before
        assert dev.sensitivity == Sensitivity.MEDIUM

    _run(main())


def test_apply_profile_validates_only_the_selected_sections():
    async def main():
        dev = _dev(apply_delay=None)  # the check below reads the device state right after the write
        ms = await _connect(dev)
        # a source that reports an out-of-range sensitivity (e.g. 0) still clones its thresholds
        profile = ConfigProfile(sensitivity=0, zone_thresholds=PAIRS)
        assert await ms.apply_profile(profile, ["zone_thresholds"]) == ["zone_thresholds"]
        assert dev.thresholds == PAIRS
        with pytest.raises(ProfileError):  # but never writes the bad section itself
            await ms.apply_profile(profile)
        assert dev.sensitivity != 0

    _run(main())


# -- calibration ---------------------------------------------------------------------


def test_calibration_keepalive_error_status_is_not_link_loss():
    async def main():
        dev = _dev(speed=100, calibration_secs=30)
        ms = await _connect(dev)
        real_ping = ms.ping
        calls = []

        async def flaky_ping(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise MS605DeviceError(7, 1)  # answered, but with an error status
            await real_ping(**kwargs)

        ms.ping = flaky_ping
        assert await ms.start_auto_calibration(timeout=5, keepalive_interval=0.02) is True
        assert len(calls) >= 2

    _run(main())


def test_stale_tag62_before_start_ack_is_not_taken_as_the_result():
    async def main():
        dev = _dev(speed=100, calibration_secs=20, calibration_result=0)
        ms = await _connect(dev)
        dev.response_delay = 1.0  # the start ACK lags 10 ms behind the write
        task = asyncio.create_task(ms.start_auto_calibration(timeout=5, keepalive_interval=10))
        await _until(lambda: dev.calibrating)
        dev._push([(TAG_SPACE_LEARNING_RESULT, b"\x01")])  # leftover success from an aborted run
        assert await task is False  # this run's own (failed) result

    _run(main())


def _start_ack_and_result(start, result: bytes, *, result_first: bool = False) -> bytearray:
    """One notification carrying the ACK of `start` and a tag62 push."""
    ack = build_frame_raw([(TAG_STATUS, b"\x00")], start.msg_id, trigger_src=start.trigger_src)
    push = build_frame_raw([(TAG_SPACE_LEARNING_RESULT, result)], 0, trigger_src=PUSH_TRIGGER_SRC)
    return bytearray(push + ack if result_first else ack + push)


def test_tag62_in_the_same_notification_right_after_the_start_ack_is_the_result():
    async def main():
        dev = _dev(calibration_secs=1000)  # the sim's own result never comes in time
        ms = await _connect(dev)
        dev.drop_responses(1)  # the start ACK is sent below, bundled with the result
        task = asyncio.create_task(ms.start_auto_calibration(timeout=1.0, keepalive_interval=10))
        await _until(lambda: dev.calibrating)
        ms._on_notify(None, _start_ack_and_result(dev.frames_in[-1], b"\x01"))
        assert await asyncio.wait_for(task, 0.5) is True  # promptly, not after the timeout

    _run(main())


def test_tag62_queued_right_behind_the_start_ack_is_the_result():
    async def main():
        dev = _dev(calibration_secs=1000)
        real_respond = dev._respond

        def respond_then_result(client, request, attrs):
            real_respond(client, request, attrs)
            if request.get(TAG_DETECT_MODE) == b"\x04":  # both dispatched before the caller resumes
                dev._push([(TAG_SPACE_LEARNING_RESULT, b"\x01")])

        dev._respond = respond_then_result
        ms = await _connect(dev)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=1.0, keepalive_interval=10))
        assert await asyncio.wait_for(task, 0.5) is True

    _run(main())


def test_tag62_strictly_before_the_start_ack_is_ignored_for_the_later_one():
    async def main():
        dev = _dev(speed=100, calibration_secs=20, calibration_result=0)
        ms = await _connect(dev)
        dev.drop_responses(1)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=5, keepalive_interval=10))
        await _until(lambda: dev.calibrating)
        # a leftover success from an aborted run, received just before this start's ACK
        ms._on_notify(None, _start_ack_and_result(dev.frames_in[-1], b"\x01", result_first=True))
        assert await task is False  # this run's own (failed) result

    _run(main())


@pytest.mark.parametrize("fault", ["corrupt_next_crc", "drop_responses"])
def test_calibration_keepalive_unanswered_on_a_live_link_is_not_link_loss(fault, caplog):
    async def main():
        dev = _dev(speed=100, calibration_secs=30)
        ms = await _connect(dev)
        ms.ping = functools.partial(ms.ping, timeout=0.05)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=5, keepalive_interval=0.02))
        await _until(lambda: dev.calibrating and not ms._pending)
        getattr(dev, fault)()  # the next keep-alive ACK is corrupted / never sent
        with caplog.at_level(logging.WARNING, logger="ms605.driver"):
            assert await task is True
        assert dev.connected

    _run(main())
    assert any("not answered" in r.getMessage() for r in caplog.records)


# -- scan RSSI -------------------------------------------------------------------------


def test_scan_exposes_rssi_for_device_menu(monkeypatch):
    handle = BLEDevice("02:00:00:00:00:07", "MRBL_SIM07", None)  # real bleak type: no rssi attribute

    class _Scanner:
        @staticmethod
        async def discover(timeout, return_adv):
            return {handle.address: (handle, SimAdvertisement(local_name=handle.name, rssi=-42))}

    monkeypatch.setattr(driver_mod, "BleakScanner", _Scanner)
    monkeypatch.setattr(MS605, "_scan_rssi", {})
    devices = _run(MS605.scan(timeout=0.01))
    assert devices == [handle]
    assert MS605.last_rssi(handle) == -42
    assert MS605.last_rssi(handle.address) == -42
    assert "RSSI -42 dBm" in _fmt_device_choice(handle)


def test_last_rssi_ignores_a_stale_scan_reading(monkeypatch):
    fleet = SimFleet(1, connectable_window=None)
    monkeypatch.setattr(driver_mod, "BleakScanner", fleet)
    monkeypatch.setattr(MS605, "_scan_rssi", {})
    address = fleet.devices[0].address
    _run(MS605.scan(timeout=0.01))
    assert MS605.last_rssi(address) == -60
    rssi, seen = MS605._scan_rssi[address]
    MS605._scan_rssi[address] = (rssi, seen - MS605._SCAN_RSSI_MAX_AGE_S - 1)
    assert MS605.last_rssi(address) is None


def test_sim_device_handle_matches_bleak_so_the_menu_uses_scan_rssi(monkeypatch):
    fleet = SimFleet(1, connectable_window=None)
    real = BLEDevice("02:00:00:00:00:01", "MRBL_SIM01", None)
    assert all(hasattr(real, f.name) for f in dataclasses.fields(fleet.devices[0].ble_device))
    monkeypatch.setattr(driver_mod, "BleakScanner", fleet)
    monkeypatch.setattr(MS605, "_scan_rssi", {})
    (handle,) = _run(MS605.scan(timeout=0.01))
    assert "RSSI -60 dBm" in _fmt_device_choice(handle)  # from the advertisement, via last_rssi()
    MS605._scan_rssi.clear()
    assert "RSSI ?" in _fmt_device_choice(handle)  # nothing on the handle itself to fall back on


# -- review follow-ups -------------------------------------------------------------


def test_request_queued_behind_a_write_is_not_sent_after_reconnect():
    async def main():
        dev = _dev()
        ms = await _connect(dev, inter_chunk_delay=0.02)
        client = ms._client
        first = asyncio.create_task(ms.set_zone_thresholds(PAIRS))  # holds the send lock mid-frame
        await _until(lambda: len(client.written) == 1)
        queued = asyncio.create_task(ms.set_sensitivity(Sensitivity.LOW))  # waits for the lock
        await asyncio.sleep(0)
        await ms.reconnect()
        for task in (first, queued):
            with pytest.raises(MS605ConnectionError):
                await asyncio.wait_for(task, 1.0)
        # told it failed, so it must not have reached the device on the new link
        assert all(f.get(61) is None for f in dev.frames_in)
        assert dev.sensitivity == Sensitivity.MEDIUM
        await ms.ping()  # the new link itself is fine

    _run(main())


def test_stalled_write_that_abandons_the_link_fires_on_disconnect():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        lost = []
        ms.on_disconnect = lambda: lost.append(ms.is_connected)
        _wedge_writes(ms._client)
        with pytest.raises(MS605TimeoutError):
            await ms.ping(timeout=0.2)
        await asyncio.sleep(0.01)  # the backend's own disconnect callback has run by now
        assert lost == [False]  # exactly once, and the link is already marked down

    _run(main())


def test_response_wait_survives_a_send_lock_wait_that_used_up_the_budget():
    async def main():
        dev = _dev(speed=100)
        dev.response_delay = 1.0  # the ACK comes 10 ms after the write
        ms = await _connect(dev)
        async with ms._send_lock:  # another frame's write holds the wire
            task = asyncio.create_task(ms.ping(timeout=0.2))
            await asyncio.sleep(0.25)  # the whole budget passes before the ping is written
        await asyncio.wait_for(task, 1.0)  # written in full, then answered: no timeout

    _run(main())


def test_one_call_timeout_covers_the_write_and_the_response_wait():
    async def main():
        dev = _dev()
        ms = await _connect(dev)
        real_write = ms._client.write_gatt_char

        async def slow_write(*args, **kwargs):
            await asyncio.sleep(0.3)
            await real_write(*args, **kwargs)

        ms._client.write_gatt_char = slow_write
        dev.drop_responses(1)
        start = time.monotonic()
        with pytest.raises(MS605TimeoutError):
            await ms.ping(timeout=0.4)
        assert time.monotonic() - start < 0.6  # was write + a full second timeout (~0.7 s)

    _run(main())


def test_cancelling_calibration_while_it_awaits_a_stuck_keepalive_propagates():
    async def main():
        dev = _dev(calibration_secs=60, live_interval=10)
        ms = await _connect(dev)
        task = asyncio.create_task(ms.start_auto_calibration(timeout=0.1, keepalive_interval=0.02))
        await _until(lambda: dev.calibrating and not ms._pending)
        _wedge_writes(ms._client)  # the next keep-alive ping sticks mid-frame
        await asyncio.sleep(0.2)  # result wait timed out; cleanup now waits on that ping
        assert not task.done()
        task.cancel()
        await ms.disconnect()  # ends the stuck write
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1.0)

    _run(main())


def test_cancelling_live_link_stop_keepalive_propagates():
    async def main():
        dev = _dev()
        session = _session(dev, 5.0)  # a stuck ping is abandoned only after 5 s
        await session.connect()
        await asyncio.sleep(0.01)  # the keep-alive loop is up
        client = session.ms._client
        _wedge_writes(client)
        released = asyncio.Event()

        async def wedged_disconnect():
            await released.wait()

        client.disconnect = wedged_disconnect  # close() blocks in its bounded disconnect
        async with session.operation("read", suspend_keepalive=True):
            pass  # releasing it pings at once
        await asyncio.sleep(0.05)  # that ping is stuck mid-frame
        stopper = asyncio.create_task(session.close())
        await asyncio.sleep(0.01)
        assert not stopper.done()
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopper, 1.0)
        # the cancelled close() still settled the keep-alive (its stuck write ended with the link)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    _run(main())


def test_profile_error_is_exported_from_the_package():
    import ms605

    assert ms605.ProfileError is ProfileError and "ProfileError" in ms605.__all__


def test_scan_mac_is_separate_from_host_uuid_and_expires(monkeypatch):
    handle = BLEDevice('00000000-0000-4000-8000-000000000007', 'ms605', None)
    adv = SimAdvertisement(local_name='ms605', rssi=-42, manufacturer_data={
        0xffff: bytes.fromhex('c01801001a0400010203190a1c00c4e7ae123456abcd00'),
    })

    class Scanner:
        @staticmethod
        async def discover(timeout, return_adv):
            return {handle.address: (handle, adv)}

    monkeypatch.setattr(driver_mod, 'BleakScanner', Scanner)
    monkeypatch.setattr(MS605, '_scan_rssi', {})
    monkeypatch.setattr(MS605, '_scan_mac', {})
    assert _run(MS605.scan(timeout=0.01)) == [handle]
    assert MS605.last_mac(handle) == 'C4:E7:AE:12:34:56'
    assert MS605.last_mac(handle.address) == 'C4:E7:AE:12:34:56'
    rssi, seen = MS605._scan_rssi[handle.address]
    MS605._scan_rssi[handle.address] = (rssi, seen - 61)
    assert MS605.last_mac(handle) is None
