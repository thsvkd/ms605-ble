"""ms605.sim -- protocol-level MS605 simulator (no radio, no hardware).

A virtual device (`SimMS605`) plus a BleakClient-compatible fake
(`SimClient`) that speaks the real wire format: written chunks are reassembled
with ms605.protocol.FrameReassembler, parsed with parse_frame(), and answered
with build_frame_raw() frames chunked back through the notify callback. Drive
the real driver against it::

    dev = SimMS605(speed=100)
    dev.press_button()
    ms = MS605(dev.address, client_factory=dev.client_factory, inter_chunk_delay=0)
    await ms.connect()

`SimFleet(n)` holds n devices and exposes a BleakScanner-shaped `discover()`
(the scanner seam: monkeypatch `ms605.driver.BleakScanner` with the fleet, or
call it directly) and a `client_factory` that routes by address.

Timing: every behaviour duration below is in *device seconds* and is divided
by `speed` to get wall-clock seconds, so speed=100 runs a 180 s calibration in
1.8 s. A duration of None disables that behaviour.

Behaviours
----------
- press_button(): opens a connectable/advertising window (`connectable_window`).
- idle drop: the device drops the link after `idle_timeout` seconds with no
  central->device writes (device->central pushes do not count).
- auto-calibration: a tag52=4 write starts SPACE_LEARNING; every
  `live_interval` the device pushes tag55 (thresholds moving linearly toward
  `learned_thresholds`) and tag56; after `calibration_secs` it stores the
  learned tag51 values and pushes tag62=`calibration_result`. A link drop
  aborts it.
- live output: tag54=1 streams tag55 every `live_interval` while connected.
- faults: drop_link(), inject_status(), corrupt_live(), drop_responses(),
  `response_delay`, corrupt_next_crc().

Measured (M0, real device; docs/GUI_PLAN.md "측정 결과", docs/SPEC.md 8.9)
--------------------------------------------------------------------------
- connectable_window=120 s after a button press: a *lower bound* (3 rounds on
  1-2 devices, never closed before the tool's 120 s cap; first connect 1-2 s
  after the press). The true upper bound is unknown. The device advertises only
  inside the window and accepts one central at a time. A connection does not
  close the window; reconnecting after it expires needs another press.
- idle_timeout=30 s without central->device traffic (29.6 s, 1 sample). A 25 s
  keep-alive interval held the link, 30 s dropped it.
- tag51 and tag61 are independent over BLE (1-2 devices): writing tag51 does
  not flip tag61 to CUSTOM (`custom_on_threshold_write=False`), and writing a
  preset level to tag61 does not change the tag51 values.
- apply_delay=1 s: a tag51 write is acked at once, but an immediate read-back
  still shows the old value; it was visible 1 s later (upper bound ~1 s, coarse).
- concurrent links: 2 of 2 available sensors held for 30 s (lower bound only;
  the sim itself has no connection limit).
- tag30 device id: 20 bytes, unique and stable on the one device observed
  (weak evidence; synthetic and shorter here).

Assumptions (simplest plausible behaviour; every MEASURE item is a placeholder
to be replaced by a real-device measurement, see docs/GUI_PLAN.md M0)
---------------------------------------------------------------------------
- MEASURE calibration_secs=180 s; on success sensitivity becomes CUSTOM(4)
  and detect mode reverts to its pre-calibration value. If the link drops
  mid-learning, learning resets (docs/GUI_PLAN.md section 1): nothing learned
  is stored and detect mode reverts; no tag62 is ever sent.
  A keep-alive does not disturb learning. Tag52 writes of 1-3 are refused
  (status error) while learning.
- MEASURE status codes: 0 = ok, STATUS_ERROR (1) for any rejected write
  (unknown tag, wrong length, out-of-range enum) or unknown read. A write
  frame gets one tag3 per written attribute; a bare ping gets a single tag3=0;
  reads answer with the tag's value and no tag3.
- MEASURE a frame with a bad CRC is ignored (no response).
- MEASURE live_interval=1 s for tag55; live output switches off on disconnect.
- Notifications use DEFAULT_CHUNK_SIZE (20-byte) chunks, i.e. no negotiated MTU.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from bleak.exc import BleakError

from .models import (
    decode_sample_intervals,
    decode_zone_thresholds,
    encode_presence_absence_times,
    encode_sample_interval,
    encode_segment_map,
    encode_zone_thresholds,
)
from .protocol import (
    DEFAULT_CHUNK_SIZE,
    NAME_PREFIXES,
    NOTIFY_CHAR_UUID,
    PUSH_TRIGGER_SRC,
    SENSITIVITY_PRESETS,
    SERVICE_UUID,
    TAG_AMBIENT_LIGHT,
    TAG_BATTERY,
    TAG_DETECT_MODE,
    TAG_DEVICE_ID,
    TAG_DND,
    TAG_LIGHT_HISTORY_COUNT,
    TAG_LIVE_OUTPUT_ENABLE,
    TAG_LIVE_RADAR_OUTPUT,
    TAG_PIR_STATE,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_PRESENCE_HISTORY_COUNT,
    TAG_READ_REQUEST,
    TAG_SAMPLE_INTERVAL,
    TAG_SEGMENT_MAP,
    TAG_SENSITIVITY,
    TAG_SPACE_LEARNING_RESULT,
    TAG_STATUS,
    TAG_SUBSENSOR_ENABLE,
    TAG_SUBSENSOR_STATUS,
    TAG_SYSTEM_SUBCOMMAND,
    TAG_TIME_SYNC,
    TAG_VERSION,
    TAG_ZONE_DISTANCES,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    WRITE_CHAR_UUID,
    DetectMode,
    FrameReassembler,
    ParsedFrame,
    Sensitivity,
    build_frame_raw,
    chunk_frame,
    parse_frame,
)

STATUS_OK = 0
STATUS_ERROR = 1  # MEASURE: real non-zero codes are unknown

# Synthetic learned thresholds (trigger, maintain) per zone -- not device data.
DEFAULT_LEARNED_THRESHOLDS: tuple[tuple[int, int], ...] = tuple(
    zip((70, 62, 55, 48, 42, 36, 30), (30, 30, 28, 26, 24, 22, 20), strict=True)
)

# Writable tags and their exact value length.
_WRITE_SIZES = {
    TAG_DND: 1,
    TAG_TIME_SYNC: 4,
    TAG_SUBSENSOR_ENABLE: 1,
    TAG_SEGMENT_MAP: 4,
    TAG_PRESENCE_ABSENCE_TIMES: 16,
    TAG_ZONE_ENABLE: 1,
    TAG_ZONE_THRESHOLDS: 28,
    TAG_DETECT_MODE: 1,
    TAG_LIVE_OUTPUT_ENABLE: 1,
    TAG_SENSITIVITY: 1,
    TAG_SAMPLE_INTERVAL: 3,
}


def _preset_pairs(level: Sensitivity) -> list[tuple[int, int]]:
    trig, maint = SENSITIVITY_PRESETS[level]
    return list(zip(trig, maint, strict=True))


def _i16(n: float) -> int:
    return max(-0x8000, min(0x7FFF, int(n)))


@dataclass
class SimBLEDevice:
    """Stand-in for bleak's BLEDevice (what MS605.scan() returns). Like
    bleak 3's, it has no rssi: that lives on the advertisement."""

    address: str
    name: str
    details: object = None


@dataclass
class SimAdvertisement:
    """Stand-in for bleak's AdvertisementData, enough for is_ms605_advertisement()."""

    local_name: str
    rssi: int
    service_uuids: list[str] = field(default_factory=lambda: [SERVICE_UUID])
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)


class SimClient:
    """BleakClient-compatible fake bound to one SimMS605 (one connection)."""

    def __init__(
        self,
        device: SimMS605,
        address_or_device: object = None,
        disconnected_callback: Callable[[SimClient], None] | None = None,
        *,
        timeout: float = 10.0,
        **_kwargs: object,
    ) -> None:
        self.device = device
        self.address = getattr(address_or_device, "address", address_or_device) or device.address
        self.timeout = timeout
        self.written: list[bytes] = []  # raw chunks, in write order
        self._disconnected_callback = disconnected_callback
        self._notify_callback: Callable[[object, bytearray], None] | None = None
        self._rx = FrameReassembler()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, **_kwargs: object) -> bool:
        self.device._accept(self)
        self._connected = True
        return True

    async def disconnect(self) -> bool:
        if self._connected:
            self.device._link_lost(self)
        return True

    async def start_notify(self, char_specifier: object, callback: Callable[[object, bytearray], None], **_kw) -> None:
        self._require_link()
        if str(char_specifier).lower() != NOTIFY_CHAR_UUID:
            raise BleakError(f"characteristic {char_specifier} does not support notify")
        self._notify_callback = callback

    async def stop_notify(self, char_specifier: object) -> None:
        self._notify_callback = None

    async def write_gatt_char(self, char_specifier: object, data: bytes, response: bool | None = None) -> None:
        self._require_link()
        if str(char_specifier).lower() != WRITE_CHAR_UUID:
            raise BleakError(f"characteristic {char_specifier} is not writable")
        self.written.append(bytes(data))
        self.device._on_write(self, bytes(data))

    def _require_link(self) -> None:
        if not self._connected:
            raise BleakError("Not connected")

    def _deliver(self, chunks: list[bytes]) -> None:
        # a frame scheduled before the link dropped must not arrive afterwards
        if not self._connected or self._notify_callback is None:
            return
        for chunk in chunks:
            self._notify_callback(NOTIFY_CHAR_UUID, bytearray(chunk))


class SimMS605:
    """Virtual MS605: config/metadata state keyed by TLV tag, timed behaviours,
    and fault injection. See the module docstring for the assumptions."""

    def __init__(
        self,
        index: int = 1,
        *,
        address: str | None = None,
        name: str | None = None,
        speed: float = 1.0,
        connectable_window: float | None = 120.0,
        idle_timeout: float | None = 30.0,
        calibration_secs: float = 180.0,
        live_interval: float = 1.0,
        learned_thresholds: Sequence[tuple[int, int]] = DEFAULT_LEARNED_THRESHOLDS,
        calibration_result: int = 1,
        custom_on_threshold_write: bool = False,
        apply_delay: float | None = 1.0,
        notify_chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be positive, got {speed}")
        self.index = index
        self.address = address or f"02:00:00:00:{(index >> 8) & 0xFF:02X}:{index & 0xFF:02X}"
        self.name = name or f"{NAME_PREFIXES[1]}SIM{index:02d}"
        self.speed = speed
        self.connectable_window = connectable_window
        self.idle_timeout = idle_timeout
        self.calibration_secs = calibration_secs
        self.live_interval = live_interval
        self.learned_thresholds = list(learned_thresholds)
        self.calibration_result = calibration_result
        self.custom_on_threshold_write = custom_on_threshold_write
        self.apply_delay = apply_delay  # device seconds until a tag51 write is readable; None = at once
        self.notify_chunk_size = notify_chunk_size
        self.response_delay = 0.0  # device seconds before each response (fault knob)
        # Per-zone live (current_trigger, current_maintain) energy; tests raise
        # these to simulate presence.
        self.signal: list[tuple[int, int]] = [(10 + 2 * z, 5 + z) for z in range(7)]
        self.frames_in: list[ParsedFrame] = []  # every frame received, in order

        # All state is raw tag bytes, exactly what a read returns.
        self.tags: dict[int, bytes] = {
            TAG_VERSION: bytes([1, 0, 0]),
            TAG_BATTERY: bytes([87]),
            TAG_DEVICE_ID: b"SIM605" + index.to_bytes(2, "big"),
            TAG_DND: b"\x00",
            TAG_TIME_SYNC: bytes(4),
            TAG_AMBIENT_LIGHT: struct.pack(">H", 120),
            TAG_SUBSENSOR_ENABLE: bytes([0b111]),
            TAG_SEGMENT_MAP: encode_segment_map([[0, 1, 2], [3, 4], [5, 6]]),
            TAG_PRESENCE_ABSENCE_TIMES: encode_presence_absence_times([(5, 30)] * 3),
            TAG_ZONE_ENABLE: bytes([0x7F]),
            TAG_ZONE_THRESHOLDS: encode_zone_thresholds(_preset_pairs(Sensitivity.MEDIUM)),
            TAG_DETECT_MODE: bytes([DetectMode.RADAR_WITH_PIR]),
            TAG_ZONE_DISTANCES: bytes([0, 8, 16, 24, 32, 40, 48, 56]),
            TAG_LIVE_OUTPUT_ENABLE: b"\x00",
            TAG_PIR_STATE: b"\x00",
            TAG_LIGHT_HISTORY_COUNT: bytes(2),
            TAG_PRESENCE_HISTORY_COUNT: bytes(2),
            TAG_SUBSENSOR_STATUS: bytes(33),  # empty mask + zeroed (synthetic) timestamps
            TAG_SENSITIVITY: bytes([Sensitivity.MEDIUM]),
            TAG_SAMPLE_INTERVAL: b"".join(encode_sample_interval(i, 60) for i in range(3)),
        }

        self._client: SimClient | None = None
        self._window_until: float | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._drop_handle: asyncio.TimerHandle | None = None
        self._ticks = 0
        self._calibrating = False
        self._calib_ticks = 0
        self._calib_total = 0
        self._calib_start: list[tuple[int, int]] = []
        self._mode_before_calibration = DetectMode.RADAR_WITH_PIR
        # fault state
        self._status_override: list[int] | None = None
        self._drop_responses = 0
        self._corrupt_crc = 0
        self._bad_live: list[str] = []

    # -- convenience views ------------------------------------------------

    @property
    def thresholds(self) -> list[tuple[int, int]]:
        return [(z.trigger, z.maintain) for z in decode_zone_thresholds(self.tags[TAG_ZONE_THRESHOLDS])]

    @property
    def sensitivity(self) -> int:
        return self.tags[TAG_SENSITIVITY][0]

    @property
    def detect_mode(self) -> int:
        return self.tags[TAG_DETECT_MODE][0]

    @property
    def calibrating(self) -> bool:
        return self._calibrating

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def advertising(self) -> bool:
        return self._client is None and self._window_open()

    @property
    def ble_device(self) -> SimBLEDevice:
        return SimBLEDevice(self.address, self.name)

    @property
    def advertisement(self) -> SimAdvertisement:
        return SimAdvertisement(local_name=self.name, rssi=-60)

    def client_factory(self, address_or_device: object = None, *args: object, **kwargs: object) -> SimClient:
        """Drop-in for the BleakClient constructor (MS605(client_factory=...))."""
        return SimClient(self, address_or_device, *args, **kwargs)

    # -- behaviours --------------------------------------------------------

    def press_button(self) -> None:
        """Open the connectable/advertising window."""
        if self.connectable_window is not None:
            self._window_until = time.monotonic() + self.connectable_window / self.speed

    def _window_open(self) -> bool:
        if self.connectable_window is None:
            return True
        return self._window_until is not None and time.monotonic() < self._window_until

    def _later(self, device_secs: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(device_secs / self.speed, callback)

    def _accept(self, client: SimClient) -> None:
        if self._client is not None:
            raise BleakError(f"{self.address} is already connected to another central")
        if not self._window_open():
            raise TimeoutError(f"connection to {self.address} timed out (simulated: button window closed)")
        self._client = client
        client._rx = FrameReassembler()
        self._touch()

    def _touch(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self.idle_timeout is not None and self._client is not None:
            self._idle_handle = self._later(self.idle_timeout, self._drop)

    def _link_lost(self, client: SimClient) -> None:
        if self._client is not client:
            return
        self._client = None
        client._connected = False
        # live output stops and learning resets below, so the ticker goes too
        for name in ("_idle_handle", "_drop_handle", "_tick_handle"):
            handle = getattr(self, name)
            if handle is not None:
                handle.cancel()
                setattr(self, name, None)
        self.tags[TAG_LIVE_OUTPUT_ENABLE] = b"\x00"
        if self._calibrating:  # learned values were never written to tag51 yet
            self._calibrating = False
            self.tags[TAG_DETECT_MODE] = bytes([self._mode_before_calibration])
        if client._disconnected_callback is not None:
            asyncio.get_running_loop().call_soon(client._disconnected_callback, client)

    def _drop(self) -> None:
        if self._client is not None:
            self._link_lost(self._client)

    def _ensure_ticker(self) -> None:
        live = self._client is not None and self.tags[TAG_LIVE_OUTPUT_ENABLE][0]
        if self._tick_handle is None and (self._calibrating or live):
            self._tick_handle = self._later(self.live_interval, self._tick)

    def _tick(self) -> None:
        self._tick_handle = None
        self._ticks += 1
        if self._calibrating:
            self._calib_ticks += 1
            self._push([(TAG_LIVE_RADAR_OUTPUT, self._radar_value())])
            self._push([(TAG_PIR_STATE, self.tags[TAG_PIR_STATE])])
            if self._calib_ticks >= self._calib_total:
                self._finish_calibration()
        elif self.tags[TAG_LIVE_OUTPUT_ENABLE][0]:
            self._push([(TAG_LIVE_RADAR_OUTPUT, self._radar_value())])
        self._ensure_ticker()

    def _start_calibration(self) -> None:
        if self._calibrating:
            return
        self._calibrating = True
        self._calib_ticks = 0
        self._calib_total = max(1, round(self.calibration_secs / self.live_interval))
        self._calib_start = self.thresholds
        self._mode_before_calibration = self.detect_mode
        self.tags[TAG_DETECT_MODE] = bytes([DetectMode.SPACE_LEARNING])
        self._ensure_ticker()

    def _finish_calibration(self) -> None:
        self._calibrating = False
        if self.calibration_result == 1:
            self.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(self.learned_thresholds)
            self.tags[TAG_SENSITIVITY] = bytes([Sensitivity.CUSTOM])
        self.tags[TAG_DETECT_MODE] = bytes([self._mode_before_calibration])
        self._push([(TAG_SPACE_LEARNING_RESULT, bytes([self.calibration_result]))])

    def _current_thresholds(self) -> list[tuple[int, int]]:
        if not self._calibrating:
            return self.thresholds
        k = self._calib_ticks / self._calib_total
        return [
            (round(t0 + (t1 - t0) * k), round(m0 + (m1 - m0) * k))
            for (t0, m0), (t1, m1) in zip(self._calib_start, self.learned_thresholds, strict=True)
        ]

    def _radar_value(self) -> bytes:
        """Tag 55 payload in the layout decode_radar_output() reads."""
        enabled = self.tags[TAG_ZONE_ENABLE][0]
        active_mask = 0
        records = b""
        for z, ((cur_t, cur_m), (thr_t, thr_m)) in enumerate(zip(self.signal, self._current_thresholds(), strict=True)):
            cur_t += self._ticks % 3  # small deterministic jitter so the stream visibly moves
            on = bool(enabled & (1 << z))
            active = on and cur_t >= thr_t
            active_mask |= active << z
            records += struct.pack(">hhBBhh", _i16(cur_t), _i16(cur_m), on, active, _i16(thr_t), _i16(thr_m))
        presence = sum(1 << i for i, mask in enumerate(self.tags[TAG_SEGMENT_MAP][:3]) if mask & active_mask)
        return bytes([presence]) + records

    # -- fault injection ------------------------------------------------------

    def drop_link(self, after: float | None = None) -> None:
        """Peer-initiated disconnect, now or after `after` device seconds."""
        if after is None:
            self._drop()
            return
        if self._drop_handle is not None:
            self._drop_handle.cancel()
        self._drop_handle = self._later(after, self._drop)

    def inject_status(self, *statuses: int) -> None:
        """Answer the next frame that writes anything with exactly these tag3
        statuses (e.g. 0, 5, 0). Write attribute i is applied only when
        statuses[i] == 0."""
        self._status_override = list(statuses)

    def drop_responses(self, count: int = 1) -> None:
        """Process but never answer the next `count` command frames."""
        self._drop_responses += count

    def corrupt_next_crc(self) -> None:
        """Flip the CRC of the next response frame."""
        self._corrupt_crc += 1

    def corrupt_live(self, mode: str = "short") -> None:
        """Damage the next tag55 push: 'short' truncates the value to 10 bytes
        (frame stays valid); 'garbled' breaks the TLV length (parse fails)."""
        if mode not in ("short", "garbled"):
            raise ValueError(f"mode must be 'short' or 'garbled', got {mode!r}")
        self._bad_live.append(mode)

    # -- frame handling ---------------------------------------------------------

    def _on_write(self, client: SimClient, chunk: bytes) -> None:
        self._touch()
        for raw in client._rx.feed(chunk):
            try:
                frame = parse_frame(raw)
            except Exception:  # noqa: BLE001 - a real device ignores noise
                continue
            self._handle(client, frame)

    def _handle(self, client: SimClient, frame: ParsedFrame) -> None:
        self.frames_in.append(frame)
        if not frame.crc_ok:
            return
        attrs = [(t, v) for t, v in frame.attributes if t != TAG_SYSTEM_SUBCOMMAND]
        overrides = None
        if self._status_override is not None and any(t != TAG_READ_REQUEST for t, _ in attrs):
            overrides, self._status_override = self._status_override, None

        out: list[tuple[int, bytes]] = []
        statuses: list[int] = []
        for tag, value in attrs:
            if tag == TAG_READ_REQUEST:
                out.append(self._read(value))
                continue
            i = len(statuses)
            if overrides is None:
                statuses.append(self._write(tag, value))
            else:
                if i < len(overrides) and overrides[i] == STATUS_OK:
                    self._write(tag, value)
                statuses.append(STATUS_ERROR)
        out += [(TAG_STATUS, bytes([s])) for s in (statuses if overrides is None else overrides)]
        if not out:  # bare ping
            out = [(TAG_STATUS, bytes([STATUS_OK]))]
        self._respond(client, frame, out)

    def _read(self, value: bytes) -> tuple[int, bytes]:
        if len(value) == 1 and value[0] in self.tags:
            return value[0], self.tags[value[0]]
        return TAG_STATUS, bytes([STATUS_ERROR])

    def _write(self, tag: int, value: bytes) -> int:
        if _WRITE_SIZES.get(tag) != len(value):
            return STATUS_ERROR
        if tag in (TAG_SENSITIVITY, TAG_DETECT_MODE) and not 1 <= value[0] <= 4:
            return STATUS_ERROR
        if tag == TAG_DETECT_MODE:
            if value[0] == DetectMode.SPACE_LEARNING:
                self._start_calibration()
                return STATUS_OK
            if self._calibrating:
                return STATUS_ERROR
        if tag == TAG_SAMPLE_INTERVAL:
            intervals = decode_sample_intervals(self.tags[TAG_SAMPLE_INTERVAL])
            intervals[value[0]] = struct.unpack(">H", value[1:])[0]
            value = b"".join(encode_sample_interval(i, s) for i, s in sorted(intervals.items()))
        if tag == TAG_ZONE_THRESHOLDS and self.apply_delay:
            self._later(self.apply_delay, lambda: self.tags.__setitem__(TAG_ZONE_THRESHOLDS, value))
        else:
            self.tags[tag] = value
        if tag == TAG_ZONE_THRESHOLDS and self.custom_on_threshold_write:
            self.tags[TAG_SENSITIVITY] = bytes([Sensitivity.CUSTOM])
        if tag == TAG_LIVE_OUTPUT_ENABLE:
            self._ensure_ticker()
        return STATUS_OK

    def _respond(self, client: SimClient, request: ParsedFrame, attrs: list[tuple[int, bytes]]) -> None:
        if self._drop_responses:
            self._drop_responses -= 1
            return
        frame = build_frame_raw(attrs, request.msg_id, trigger_src=request.trigger_src)
        if self._corrupt_crc:
            self._corrupt_crc -= 1
            frame = frame[:-4] + bytes([frame[-4] ^ 0xFF]) + frame[-3:]
        chunks = chunk_frame(frame, self.notify_chunk_size)
        loop = asyncio.get_running_loop()
        if self.response_delay:
            loop.call_later(self.response_delay / self.speed, client._deliver, chunks)
        else:
            loop.call_soon(client._deliver, chunks)

    def _push(self, attrs: list[tuple[int, bytes]]) -> None:
        client = self._client
        if client is None:
            return  # nobody listening: the push is lost
        mode = None
        if attrs[0][0] == TAG_LIVE_RADAR_OUTPUT and self._bad_live:
            mode = self._bad_live.pop(0)
            if mode == "short":
                attrs = [(TAG_LIVE_RADAR_OUTPUT, attrs[0][1][:10])]
        frame = build_frame_raw(attrs, 0, trigger_src=PUSH_TRIGGER_SRC)
        if mode == "garbled":
            frame = frame[:8] + b"\xff\xff" + frame[10:]  # first TLV claims 65535 bytes
        asyncio.get_running_loop().call_soon(client._deliver, chunk_frame(frame, self.notify_chunk_size))


class SimFleet:
    """N virtual devices behind one scanner seam and one client factory."""

    def __init__(self, count: int = 0, **device_kwargs: object) -> None:
        self.devices = [SimMS605(i + 1, **device_kwargs) for i in range(count)]

    def device(self, address: str) -> SimMS605:
        for dev in self.devices:
            if dev.address.lower() == address.lower():
                return dev
        raise KeyError(address)

    def press_all(self) -> None:
        for dev in self.devices:
            dev.press_button()

    async def discover(self, timeout: float = 5.0, *, return_adv: bool = False, **_kwargs: object):
        """BleakScanner.discover() shape: only advertising devices are listed."""
        await asyncio.sleep(0)
        found = [d for d in self.devices if d.advertising]
        if return_adv:
            return {d.address: (d.ble_device, d.advertisement) for d in found}
        return [d.ble_device for d in found]

    def client_factory(self, address_or_device: object, *args: object, **kwargs: object) -> SimClient:
        address = str(getattr(address_or_device, "address", address_or_device))
        try:
            dev = self.device(address)
        except KeyError:
            raise BleakError(f"no such device: {address}") from None
        return dev.client_factory(address_or_device, *args, **kwargs)
