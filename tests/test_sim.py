"""The real ms605.MS605 driver against the protocol-level simulator (ms605.sim).
No BLE hardware: the simulator speaks the wire format through a fake
BleakClient. Every value here is synthetic."""

from __future__ import annotations

import asyncio

import pytest

import ms605.driver as driver_mod
from ms605 import MS605, MS605ConnectionError, MS605DeviceError, MS605TimeoutError
from ms605.errors import FrameError
from ms605.models import decode_radar_output
from ms605.protocol import (
    NOTIFY_CHAR_UUID,
    SENSITIVITY_PRESETS,
    TAG_BATTERY,
    TAG_DEVICE_ID,
    TAG_LIVE_RADAR_OUTPUT,
    TAG_PIR_STATE,
    TAG_READ_REQUEST,
    TAG_SENSITIVITY,
    TAG_STATUS,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    WRITE_CHAR_UUID,
    DetectMode,
    FrameReassembler,
    Sensitivity,
    build_command,
    chunk_frame,
    parse_frame,
)
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, STATUS_ERROR, SimFleet, SimMS605

PAIRS = [(90, 41), (80, 41), (70, 41), (60, 41), (50, 41), (40, 31), (30, 21)]


def _run(coro):
    return asyncio.run(coro)


async def _settle(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _driver(dev: SimMS605, *, press: bool = True) -> MS605:
    if press:
        dev.press_button()
    ms = MS605(dev.address, client_factory=dev.client_factory, inter_chunk_delay=0)
    await ms.connect()
    return ms


class _Raw:
    """A bare SimClient session that records every parsed notification frame."""

    def __init__(self, dev: SimMS605, **kwargs) -> None:
        self.client = dev.client_factory(dev.address, **kwargs)
        self.raw: list[bytes] = []
        self._ra = FrameReassembler()
        self._msg_id = 0

    async def open(self) -> _Raw:
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR_UUID, lambda _c, d: self.raw.extend(self._ra.feed(bytes(d))))
        return self

    async def send(self, attrs, *, frame: bytes | None = None):
        self._msg_id += 1
        for chunk in chunk_frame(frame or build_command(attrs, self._msg_id)):
            await self.client.write_gatt_char(WRITE_CHAR_UUID, chunk, response=False)
        await _settle()


def _fast(**kw) -> SimMS605:
    # timers off unless a test opts in; speed only matters for timed behaviours
    kw.setdefault("connectable_window", None)
    kw.setdefault("idle_timeout", None)
    kw.setdefault("speed", 3600)
    return SimMS605(**kw)


# -- connection / button window -------------------------------------------------


def test_connect_requires_button_window_and_window_expires():
    async def run():
        dev = SimMS605(connectable_window=30, idle_timeout=None, speed=1000)  # 30 ms window
        ms = MS605(dev.address, client_factory=dev.client_factory, inter_chunk_delay=0)
        assert not dev.advertising
        with pytest.raises(MS605ConnectionError):
            await ms.connect()
        dev.press_button()
        assert dev.advertising
        await ms.connect()
        assert ms.is_connected and dev.connected and not dev.advertising
        await ms.disconnect()
        assert not dev.connected
        await asyncio.sleep(0.05)  # window (30 ms) has closed
        with pytest.raises(MS605ConnectionError):
            await ms.connect()

    _run(run())


def test_second_central_is_refused():
    async def run():
        dev = _fast()
        ms = await _driver(dev)
        with pytest.raises(MS605ConnectionError):
            await _driver(dev)
        assert ms.is_connected

    _run(run())


# -- read / write semantics ---------------------------------------------------------


def test_read_config_reflects_device_state():
    async def run():
        dev = _fast()
        ms = await _driver(dev)
        cfg = await ms.read_config()
        assert cfg.sensitivity == Sensitivity.MEDIUM
        assert cfg.detect_mode == DetectMode.RADAR_WITH_PIR
        assert [(z.trigger, z.maintain) for z in cfg.zone_thresholds] == dev.thresholds
        assert cfg.zones_enabled() == (True,) * 7
        assert [zm.zones for zm in cfg.segment_map] == [(0, 1, 2), (3, 4), (5, 6)]

    _run(run())


def test_write_then_verify_thresholds_and_preset_reload():
    async def run():
        dev = _fast()
        ms = await _driver(dev)
        await ms.set_zone_thresholds(PAIRS)
        cfg = await ms.read_config()
        assert [(z.trigger, z.maintain) for z in cfg.zone_thresholds] == PAIRS
        assert cfg.sensitivity == Sensitivity.MEDIUM  # tag51 alone does not flip tag61 by default
        await ms.set_sensitivity(Sensitivity.HIGH)  # preset level reloads its table
        trig, maint = SENSITIVITY_PRESETS[Sensitivity.HIGH]
        assert dev.thresholds == list(zip(trig, maint, strict=True))
        await ms.set_zone_enable([True, False] * 3 + [True])
        assert dev.tags[TAG_ZONE_ENABLE] == bytes([0b1010101])

    _run(run())


def test_threshold_write_can_flip_sensitivity_to_custom():
    async def run():
        dev = _fast(custom_on_threshold_write=True)
        ms = await _driver(dev)
        await ms.set_zone_thresholds(PAIRS)
        assert dev.sensitivity == Sensitivity.CUSTOM

    _run(run())


def test_ping_and_metadata_reads():
    async def run():
        dev = _fast(index=7)
        ms = await _driver(dev)
        await ms.ping()
        assert dev.frames_in[-1].attributes == [(1, b"\x06")]
        frame = await ms.read_raw([TAG_DEVICE_ID, TAG_BATTERY])
        assert frame.get(TAG_DEVICE_ID) == dev.tags[TAG_DEVICE_ID]
        assert frame.get(TAG_BATTERY) == bytes([87])
        await ms.set_dnd(True)
        assert await ms.read_dnd() is True

    _run(run())


def test_invalid_write_and_unknown_read_get_error_status():
    async def run():
        dev = _fast()
        raw = await _Raw(dev).open()
        before = dev.tags[TAG_ZONE_THRESHOLDS]
        await raw.send([(TAG_ZONE_THRESHOLDS, b"\x00" * 5), (TAG_SENSITIVITY, b"\x09")])
        await raw.send([(TAG_READ_REQUEST, bytes([200]))])
        bad_write, bad_read = (parse_frame(f) for f in raw.raw)
        assert bad_write.get_all(TAG_STATUS) == [bytes([STATUS_ERROR])] * 2
        assert bad_read.status() == STATUS_ERROR
        assert dev.tags[TAG_ZONE_THRESHOLDS] == before

    _run(run())


# -- timed behaviours ---------------------------------------------------------------


def test_idle_link_drop_fires_disconnected_callback():
    async def run():
        dev = _fast(idle_timeout=30, speed=1000)  # 30 ms without writes
        seen = []
        raw = await _Raw(dev, disconnected_callback=seen.append).open()
        await asyncio.sleep(0.06)
        assert not raw.client.is_connected and not dev.connected
        assert seen == [raw.client]

    _run(run())


def test_auto_calibration_streams_pushes_and_updates_thresholds():
    async def run():
        dev = _fast()  # 180 s at 3600x = 50 ms
        ms = await _driver(dev)
        pushes = []
        ms.add_push_handler(pushes.append)
        assert await ms.start_auto_calibration(timeout=2.0) is True
        assert dev.thresholds == list(DEFAULT_LEARNED_THRESHOLDS)
        assert dev.sensitivity == Sensitivity.CUSTOM
        assert dev.detect_mode == DetectMode.RADAR_WITH_PIR  # restored after learning
        radar = [decode_radar_output(p.get(TAG_LIVE_RADAR_OUTPUT)) for p in pushes if p.get(TAG_LIVE_RADAR_OUTPUT)]
        assert len(radar) == 180 and any(p.get(TAG_PIR_STATE) is not None for p in pushes)
        # thresholds stream from the old table toward the learned one
        assert radar[0].zones[0].trigger_threshold > radar[-1].zones[0].trigger_threshold
        assert radar[-1].zones[0].trigger_threshold == DEFAULT_LEARNED_THRESHOLDS[0][0]

    _run(run())


def test_failed_calibration_reports_false_and_keeps_thresholds():
    async def run():
        dev = _fast(calibration_result=0)
        ms = await _driver(dev)
        before = dev.thresholds
        assert await ms.start_auto_calibration(timeout=2.0) is False
        assert dev.thresholds == before

    _run(run())


def test_live_output_streams_on_demand_and_stops():
    async def run():
        dev = _fast(speed=1000)  # one tag55 per ms
        dev.signal[0] = (500, 300)  # strong energy in zone 0 -> sub-sensor 0 present
        ms = await _driver(dev)
        snaps = []
        ms.add_push_handler(lambda f: snaps.append(decode_radar_output(f.get(TAG_LIVE_RADAR_OUTPUT))))
        await ms.set_live_output(True)
        await asyncio.sleep(0.03)
        await ms.set_live_output(False)
        count = len(snaps)
        await asyncio.sleep(0.01)
        assert count >= 3 and len(snaps) == count
        assert snaps[-1].zones[0].trigger_active and snaps[-1].sub_sensor_presence[0]

    _run(run())


# -- fault injection ------------------------------------------------------------------


def test_drop_link_now_disconnects_and_writes_fail():
    async def run():
        dev = _fast()
        seen = []
        raw = await _Raw(dev, disconnected_callback=seen.append).open()
        dev.drop_link()
        await _settle()
        assert not raw.client.is_connected and seen == [raw.client]
        with pytest.raises(Exception, match="Not connected"):
            await raw.send([])

    _run(run())


def test_drop_link_mid_calibration_fails_fast_and_device_abandons_learning():
    async def run():
        dev = _fast()
        ms = await _driver(dev)
        before = (dev.thresholds, dev.sensitivity, dev.detect_mode)
        dev.drop_link(after=30)  # learning itself takes >= 50 ms
        with pytest.raises(MS605ConnectionError):
            await ms.start_auto_calibration(timeout=2.0, keepalive_interval=0.02)
        assert not dev.calibrating  # learning resets on disconnect (docs/GUI_PLAN.md section 1)
        await asyncio.sleep(0.1)  # well past the 50 ms learning time: nothing is committed later
        assert (dev.thresholds, dev.sensitivity, dev.detect_mode) == before

    _run(run())


def test_bad_status_on_next_write_raises_and_is_not_applied():
    async def run():
        dev = _fast()
        ms = await _driver(dev)
        dev.inject_status(5)
        with pytest.raises(MS605DeviceError) as err:
            await ms.set_sensitivity(Sensitivity.LOW)
        assert err.value.status == 5
        assert dev.sensitivity == Sensitivity.MEDIUM
        await ms.set_sensitivity(Sensitivity.LOW)  # one-shot fault
        assert dev.sensitivity == Sensitivity.LOW

    _run(run())


def test_multi_status_response_applies_only_ok_writes():
    async def run():
        dev = _fast()
        raw = await _Raw(dev).open()
        dev.inject_status(0, 5, 0)
        await raw.send([(TAG_SENSITIVITY, b"\x03"), (TAG_ZONE_ENABLE, b"\x01"), (TAG_SENSITIVITY, b"\x01")])
        assert parse_frame(raw.raw[0]).get_all(TAG_STATUS) == [b"\x00", b"\x05", b"\x00"]
        assert dev.sensitivity == Sensitivity.LOW and dev.tags[TAG_ZONE_ENABLE] == b"\x7f"

    _run(run())


def test_short_and_garbled_tag55():
    async def run():
        dev = _fast(speed=1000)
        ms = await _driver(dev)
        decoded, errors = [], []

        def handler(frame):
            try:
                decoded.append(decode_radar_output(frame.get(TAG_LIVE_RADAR_OUTPUT)))
            except FrameError as exc:
                errors.append(exc)

        ms.add_push_handler(handler)
        dev.corrupt_live("short")
        dev.corrupt_live("garbled")
        await ms.set_live_output(True)
        while len(decoded) < 2:
            await asyncio.sleep(0.001)
        await ms.set_live_output(False)
        assert len(errors) == 1  # short value reached the handler and failed to decode
        assert dev._ticks >= len(decoded) + 2  # the garbled frame never reached it

    _run(run())


def test_garbled_tag55_frame_is_structurally_broken():
    async def run():
        dev = _fast(speed=1000)
        raw = await _Raw(dev).open()
        dev.corrupt_live("garbled")
        await raw.send([(54, b"\x01")])
        while len(raw.raw) < 2:
            await asyncio.sleep(0.001)
        with pytest.raises(FrameError):
            parse_frame(raw.raw[1])

    _run(run())


def test_dropped_and_delayed_responses():
    async def run():
        dev = _fast(speed=100)
        ms = await _driver(dev)
        dev.drop_responses(1)
        with pytest.raises(MS605TimeoutError):
            await ms.ping(timeout=0.02)
        await ms.ping(timeout=0.5)  # next one is answered
        dev.response_delay = 2.0  # 20 ms at 100x
        with pytest.raises(MS605TimeoutError):
            await ms.ping(timeout=0.005)
        await ms.ping(timeout=0.5)

    _run(run())


def test_corrupted_crc_response():
    async def run():
        dev = _fast()
        raw = await _Raw(dev).open()
        dev.corrupt_next_crc()
        await raw.send([])
        await raw.send([])
        first, second = (parse_frame(f) for f in raw.raw)
        assert not first.crc_ok and second.crc_ok

    _run(run())


def test_bad_crc_request_is_ignored():
    async def run():
        dev = _fast()
        raw = await _Raw(dev).open()
        frame = bytearray(build_command([(TAG_SENSITIVITY, b"\x01")], 1))
        frame[-4] ^= 0xFF
        await raw.send([], frame=bytes(frame))
        assert raw.raw == [] and dev.sensitivity == Sensitivity.MEDIUM

    _run(run())


# -- fleet / scanner seam ---------------------------------------------------------------


def test_fleet_scan_and_connect_through_real_driver(monkeypatch):
    async def run():
        # the default 30s window would be ~8ms of wall clock at speed=3600; keep it ~30s
        fleet = SimFleet(3, idle_timeout=None, connectable_window=30 * 3600, speed=3600)
        monkeypatch.setattr(driver_mod, "BleakScanner", fleet)
        assert await MS605.scan(timeout=0.1) == []  # nobody pressed the button
        fleet.press_all()
        found = await MS605.scan(timeout=0.1)
        assert sorted(d.address for d in found) == sorted(d.address for d in fleet.devices)
        ids = set()
        for handle in found:
            ms = MS605(handle, client_factory=fleet.client_factory, inter_chunk_delay=0)
            await ms.connect()
            ids.add((await ms.read_raw([TAG_DEVICE_ID])).get(TAG_DEVICE_ID))
        assert len(ids) == 3
        assert await MS605.scan(timeout=0.1) == []  # connected devices stop advertising
        with pytest.raises(MS605ConnectionError):
            await MS605("02:00:00:00:FF:FF", client_factory=fleet.client_factory).connect()

    _run(run())
