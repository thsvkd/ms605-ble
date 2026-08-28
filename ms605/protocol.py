"""ms605.protocol -- binary TLV frame format for the Meross MS605 (com.meross.ble2).

PROTOCOL (an original implementation of the derived interoperability
specification in docs/SPEC.md). The repository publishes the method,
specification, implementation, and synthetic tests; it does not distribute
vendor source or raw device captures.

GATT profile
------------
The MS605 does NOT use the legacy Meross BLE stack (service 0000A00A, JSON
framing). It uses a second, separate stack (com.meross.ble2) with its own
GATT service::

    service : 99E7BE30-0001-4C6B-98A2-70FCB3471A72
    write   : 99E7BE30-0002-4C6B-98A2-70FCB3471A72   (WRITE_TYPE_NO_RESPONSE)
    notify  : 99E7BE30-0003-4C6B-98A2-70FCB3471A72   (subscribe via CCCD 2902)

It advertises manufacturer-specific data under company id 0xFFFF whose first
byte ("subdevType") is 0xC0, and/or a local name prefixed "RFBL_"/"MRBL_".

Frame format (binary TLV, NOT JSON)
------------------------------------
Command frame, app -> device::

    55 AA | subdevType(1)=0xC0 | length(2 BE) | triggerSrc(1)=0x11 | msgId(1)
          | TLV attr ... | CRC16(2 BE) | AA 55

    TLV attr := tag(1) | valueLen(2 BE) | value(valueLen bytes)

- `length` = byte count of everything between the length field and the CRC,
  i.e. `triggerSrc + msgId + all TLV attrs`.
- `CRC16` is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no
  xorout), computed over the same span the length header covers
  (`frame[5:-4]`), stored big-endian.
- `msgId` rolls 1..255 per outgoing command; 0 is reserved and marks an
  unsolicited device->app PUSH frame (`triggerSrc` is then 0x00 too).
  Command *responses* echo the request's msgId.
- Commands carry a trailing TLV `tag=1 (system sub-command), len=1,
  value=0x06`; `build_command()` appends it automatically. Device-to-client
  responses and pushes do not use this command trailer.
- Values are big-endian unless otherwise noted below.
- A response's TLV `tag=3` (status) is 0 on success, non-zero on error.
- Because every frame is self-delimited (55AA/length/AA55), it can be split
  into MTU-3-byte chunks (20 bytes at the default/unnegotiated ATT MTU of 23)
  and written sequentially with WRITE_TYPE_NO_RESPONSE; the device reassembles
  by the declared length header.

Tag table (decimal tag values; each stored as a single byte on the wire) --
see docs/SPEC.md for the public contract and confidence labels. Implemented
tags are enumerated below as TAG_* constants; see ms605.driver.MS605 for the
operations exposed by the driver.

Sensitivity presets (trigger[7], maintain[7]) retained as reference values for
the non-CUSTOM levels; see SENSITIVITY_PRESETS:
    LOW (1):    trig=[75,65,55,50,45,35,30]   maint=[30,30,30,30,30,30,25]
    MEDIUM (2): trig=[95,85,75,60,55,40,35]   maint=[40,40,40,40,40,35,28]  (device default)
    HIGH (3):   trig=[105,95,85,70,65,50,45]  maint=[50,50,50,50,50,45,40]

Synthetic golden frames (generated and verified by tests/test_protocol.py)
-------------------------------------------------------------------------
    set sensitivity=4, msgId=0x2A:
        55aac0000a112a3d00010401000106abd3aa55
    start auto-calibration (tag52=4), msgId=0x2B:
        55aac0000a112b340001040100010619f9aa55
    space-learning result push (tag62=1):
        55aac0000600003e000101a3b3aa55

Safety note
-----------
The MS605 only accepts a BLE GATT connection for a short window right after
its physical button is pressed, gated by the device owner. This module's
frame builders/parsers are pure functions with no I/O and are fully testable
offline; only ms605.driver touches a real radio.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import IntEnum

from .errors import FrameError

# ---------------------------------------------------------------------------
# GATT / advertisement constants
# ---------------------------------------------------------------------------

SERVICE_UUID = "99e7be30-0001-4c6b-98a2-70fcb3471a72"
WRITE_CHAR_UUID = "99e7be30-0002-4c6b-98a2-70fcb3471a72"
NOTIFY_CHAR_UUID = "99e7be30-0003-4c6b-98a2-70fcb3471a72"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

SUBDEV_TYPE = 0xC0  # MS605 device-type byte ("subdevType")
TRIGGER_SRC = 0x11  # on-wire triggerSrc stamped on every outgoing command
PUSH_TRIGGER_SRC = 0x00  # triggerSrc on unsolicited device->app pushes

ADV_COMPANY_ID = 0xFFFF
NAME_PREFIXES = ("RFBL_", "MRBL_")

MAGIC_HEAD = b"\x55\xaa"
MAGIC_TAIL = b"\xaa\x55"

DEFAULT_MTU = 23
DEFAULT_CHUNK_SIZE = DEFAULT_MTU - 3  # 20-byte ATT payload at the default MTU
INTER_CHUNK_DELAY_S = 0.02  # conservative gap between sequential writes
WRITE_TIMEOUT_S = 10.0
CALIBRATION_TIMEOUT_S = 200.0

# ---------------------------------------------------------------------------
# TLV tags
# ---------------------------------------------------------------------------

TAG_SYSTEM_SUBCOMMAND = 1
TAG_READ_REQUEST = 2
TAG_STATUS = 3
TAG_DEVICE_ID = 30
TAG_VERSION = 21  # firmware/supported-tags blob; exact semantics are ambiguous
TAG_BATTERY = 23
TAG_DND = 32
TAG_TIME_SYNC = 33
TAG_AMBIENT_LIGHT = 36
TAG_SUBSENSOR_ENABLE = 41
TAG_SEGMENT_MAP = 48
TAG_PRESENCE_ABSENCE_TIMES = 49
TAG_ZONE_ENABLE = 50
TAG_ZONE_THRESHOLDS = 51
TAG_DETECT_MODE = 52
TAG_ZONE_DISTANCES = 53
TAG_LIVE_OUTPUT_ENABLE = 54
TAG_LIVE_RADAR_OUTPUT = 55
TAG_PIR_STATE = 56
TAG_LIGHT_HISTORY_COUNT = 57
TAG_PRESENCE_HISTORY_PUSH = 58
TAG_PRESENCE_HISTORY_COUNT = 59
TAG_LIGHT_HISTORY_PUSH = 60
TAG_SENSITIVITY = 61
TAG_SPACE_LEARNING_RESULT = 62
TAG_SUBSENSOR_STATUS = 64
TAG_SAMPLE_INTERVAL = 98

SYSTEM_SUBCOMMAND_VALUE = 0x06  # mandatory trailer value

# Stable order used by read_config().
READ_CONFIG_TAGS: tuple[int, ...] = (
    TAG_SUBSENSOR_ENABLE,
    TAG_SEGMENT_MAP,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    TAG_DETECT_MODE,
    TAG_ZONE_DISTANCES,
    TAG_SENSITIVITY,
)

TAG_NAMES = {
    1: "system sub-command",
    2: "READ request (value=tag)",
    3: "status/error",
    21: "version / supported-tags blob",
    23: "battery",
    30: "device id",
    32: "DND (do-not-disturb)",
    33: "time sync",
    36: "ambient light",
    41: "sub-sensor enable bitmask",
    48: "sub-sensor segment map",
    49: "sub-sensor presence/absence times",
    50: "zone enable bitmask",
    51: "zone thresholds (28B)",
    52: "detect mode / space-learning",
    53: "zone distances (0.1m)",
    54: "live-output enable",
    55: "live radar output (push)",
    56: "PIR state",
    57: "light history record count",
    58: "presence history records (push)",
    59: "presence history record count",
    60: "light history records (push)",
    61: "radar sensitivity",
    62: "space-learning result (push)",
    64: "sub-sensor status",
    98: "per-sub-sensor sample interval",
}


class Sensitivity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CUSTOM = 4


class DetectMode(IntEnum):
    RADAR = 1
    RADAR_WITH_PIR = 2
    PIR_WITH_RADAR = 3
    SPACE_LEARNING = 4


# Reference (trigger[7], maintain[7]) profiles for non-CUSTOM sensitivity
# levels. ``set_sensitivity()`` writes only tag 61; callers must write zone
# thresholds separately when they intend to change both settings.
SENSITIVITY_PRESETS: dict[Sensitivity, tuple[list[int], list[int]]] = {
    Sensitivity.LOW: ([75, 65, 55, 50, 45, 35, 30], [30, 30, 30, 30, 30, 30, 25]),
    Sensitivity.MEDIUM: ([95, 85, 75, 60, 55, 40, 35], [40, 40, 40, 40, 40, 35, 28]),
    Sensitivity.HIGH: ([105, 95, 85, 70, 65, 50, 45], [50, 50, 50, 50, 50, 45, 40]),
}


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout.

    Matches the standard check value crc16_ccitt_false(b"123456789") ==
    0x29B1."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# Frame (de)serialization
# ---------------------------------------------------------------------------

Attr = tuple[int, bytes]


def _serialize_frame(
    attrs: Sequence[Attr],
    msg_id: int,
    subdev_type: int,
    trigger_src: int,
) -> bytes:
    """Core TLV-envelope serializer, with NO trailer logic -- shared by
    build_command() (adds the tag1=06 trailer) and the internal helper used
    by tests to reconstruct device->app frames (which never carry it)."""
    if not 0 <= msg_id <= 0xFF:
        raise FrameError(f"msg_id out of byte range: {msg_id}")
    if not 0 <= subdev_type <= 0xFF:
        raise FrameError(f"subdev_type out of byte range: {subdev_type}")
    if not 0 <= trigger_src <= 0xFF:
        raise FrameError(f"trigger_src out of byte range: {trigger_src}")

    body = bytearray([trigger_src & 0xFF, msg_id & 0xFF])
    for tag, value in attrs:
        value = bytes(value)
        if not 0 <= tag <= 0xFF:
            raise FrameError(f"tag out of byte range: {tag}")
        if len(value) > 0xFFFF:
            raise FrameError(f"TLV value too large for 16-bit length: {len(value)} bytes")
        body += bytes([tag & 0xFF]) + struct.pack(">H", len(value)) + value

    length = len(body)
    crc = crc16_ccitt_false(bytes(body))
    return (
        MAGIC_HEAD
        + bytes([subdev_type & 0xFF])
        + struct.pack(">H", length)
        + bytes(body)
        + struct.pack(">H", crc)
        + MAGIC_TAIL
    )


def build_command(
    attrs: Sequence[Attr],
    msg_id: int,
    *,
    subdev_type: int = SUBDEV_TYPE,
    trigger_src: int = TRIGGER_SRC,
) -> bytes:
    """Serialize one MS605 TLV *command* frame (app -> device).

    `attrs` are the "real" attributes for this command (e.g. a single
    `(TAG_SENSITIVITY, bytes([level]))` write, or one
    `(TAG_READ_REQUEST, bytes([tag]))` per tag for a multi-read). The
    mandatory trailing `tag=1 (system sub-command), len=1, value=0x06` is
    appended automatically. It is part of this implementation's command
    contract; interoperability without it is not supported.
    """
    return _serialize_frame(
        list(attrs) + [(TAG_SYSTEM_SUBCOMMAND, bytes([SYSTEM_SUBCOMMAND_VALUE]))],
        msg_id,
        subdev_type,
        trigger_src,
    )


def build_frame_raw(
    attrs: Sequence[Attr],
    msg_id: int,
    *,
    subdev_type: int = SUBDEV_TYPE,
    trigger_src: int = TRIGGER_SRC,
) -> bytes:
    """Like build_command(), but WITHOUT the automatic tag1=06 trailer.

    Device -> app frames (responses and pushes) never carry that trailer, so
    this is what tests use to reconstruct known-good frames from their
    already-decoded attribute bytes."""
    return _serialize_frame(list(attrs), msg_id, subdev_type, trigger_src)


@dataclass
class ParsedFrame:
    subdev_type: int
    trigger_src: int
    msg_id: int
    attributes: list[Attr]
    crc_ok: bool
    expected_crc: int
    actual_crc: int
    is_push: bool  # msg_id == 0 -> unsolicited push/notification

    def get(self, tag: int) -> bytes | None:
        """First value for `tag`, or None if absent."""
        for t, v in self.attributes:
            if t == tag:
                return v
        return None

    def get_all(self, tag: int) -> list[bytes]:
        """All values for `tag` (some responses repeat tag 3 per sub-write)."""
        return [v for t, v in self.attributes if t == tag]

    def status(self) -> int | None:
        """Decoded tag-3 status byte, or None if this frame has no tag 3."""
        value = self.get(TAG_STATUS)
        return value[0] if value else None


def parse_frame(packet: bytes) -> ParsedFrame:
    """Inverse of :func:`build_command` and :func:`build_frame_raw`.

    Raises FrameError on structural problems (bad magic, truncation, bad TLV
    lengths) but reports a CRC mismatch via `.crc_ok` rather than raising --
    a bad CRC is diagnostic information, not necessarily a fatal parse error.
    """
    # 2 magic + 1 subdev + 2 len + 1 trig + 1 msgid + 2 crc + 2 magic = 11 min
    if len(packet) < 11:
        raise FrameError(f"packet too short ({len(packet)} bytes) for an MS605 TLV frame")
    if packet[0:2] != MAGIC_HEAD:
        raise FrameError(f"bad head magic: {packet[0:2].hex()} (expected 55aa)")
    if packet[-2:] != MAGIC_TAIL:
        raise FrameError(f"bad tail magic: {packet[-2:].hex()} (expected aa55)")

    subdev_type = packet[2]
    declared_length = struct.unpack(">H", packet[3:5])[0]
    body_start = 5
    body_end = body_start + declared_length
    crc_end = body_end + 2
    if crc_end + 2 != len(packet):
        raise FrameError(
            f"length mismatch: header declares {declared_length}-byte body, "
            f"inconsistent with packet size {len(packet)}"
        )

    body = packet[body_start:body_end]
    if len(body) < 2:
        raise FrameError("body too short to contain triggerSrc + msgId")
    trigger_src = body[0]
    msg_id = body[1]

    attributes: list[Attr] = []
    offset = 2
    while offset < len(body):
        if offset + 3 > len(body):
            raise FrameError(f"truncated TLV header at body offset {offset}")
        tag = body[offset]
        vlen = struct.unpack(">H", body[offset + 1 : offset + 3])[0]
        value_start = offset + 3
        value_end = value_start + vlen
        if value_end > len(body):
            raise FrameError(
                f"TLV tag {tag} declares {vlen}-byte value but only "
                f"{len(body) - value_start} byte(s) remain"
            )
        attributes.append((tag, bytes(body[value_start:value_end])))
        offset = value_end

    actual_crc = struct.unpack(">H", packet[body_end:crc_end])[0]
    expected_crc = crc16_ccitt_false(bytes(body))
    return ParsedFrame(
        subdev_type=subdev_type,
        trigger_src=trigger_src,
        msg_id=msg_id,
        attributes=attributes,
        crc_ok=(actual_crc == expected_crc),
        expected_crc=expected_crc,
        actual_crc=actual_crc,
        is_push=(msg_id == 0),
    )


# ---------------------------------------------------------------------------
# Rolling msgId
# ---------------------------------------------------------------------------


def msg_id_sequence(start: int = 1) -> Iterator[int]:
    """Infinite generator of MS605 command msgIds: rolls 1..255. 0 is
    reserved for unsolicited device pushes and must never be used as a
    command's msgId."""
    if not 1 <= start <= 255:
        raise ValueError(f"start must be in 1..255, got {start}")
    n = start
    while True:
        yield n
        n = 1 if n == 255 else n + 1


# ---------------------------------------------------------------------------
# Chunking / notify reassembly
# ---------------------------------------------------------------------------


def chunk_frame(frame: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[bytes]:
    """Split a serialized frame into `chunk_size`-byte pieces for sequential
    WRITE_TYPE_NO_RESPONSE writes (default 20 bytes = MTU(23) - 3)."""
    if chunk_size <= 0:
        raise FrameError(f"chunk_size must be positive, got {chunk_size}")
    if not frame:
        return []
    return [frame[i : i + chunk_size] for i in range(0, len(frame), chunk_size)]


class FrameReassembler:
    """Stateful reassembly of MS605 TLV frames out of a stream of raw BLE
    notification chunks.

    Pure/no I/O -- exercised directly by tests without a Bluetooth stack.
    Resynchronizes on the next 55AA magic if unexpected bytes precede a
    frame, rather than raising.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Feed one raw notification chunk; return zero or more complete
        frames (still-unparsed bytes) extracted from the accumulated
        buffer, in arrival order."""
        self._buffer += chunk
        frames: list[bytes] = []
        while True:
            idx = self._buffer.find(MAGIC_HEAD)
            if idx == -1:
                # No magic at all in the buffer; keep only a possible
                # trailing half-magic byte.
                if len(self._buffer) > 1:
                    del self._buffer[:-1]
                break
            if idx > 0:
                del self._buffer[:idx]
            if len(self._buffer) < 5:
                break  # need more bytes to read the length header
            declared_length = struct.unpack(">H", bytes(self._buffer[3:5]))[0]
            total_len = 5 + declared_length + 4  # head+subdev+len(5) + body + crc(2) + tail(2)
            if len(self._buffer) < total_len:
                break  # wait for more chunks
            frame = bytes(self._buffer[:total_len])
            del self._buffer[:total_len]
            frames.append(frame)
        return frames


# ---------------------------------------------------------------------------
# Advertisement matching support (constants used by ms605.discovery)
# ---------------------------------------------------------------------------


def is_ms605_advertisement(
    name: str | None,
    service_uuids: Iterable[str] | None,
    manufacturer_data: dict[int, bytes] | None,
) -> bool:
    """True if this advertisement looks like an MS605: the configured GATT
    service UUID, the 0xFFFF/0xC0 manufacturer-data signature, or an
    RFBL_/MRBL_ name prefix. Never raises.

    Lives in ms605.protocol (not ms605.discovery) because it only needs the
    frame-format constants above; ms605.discovery re-exports it alongside
    the BLE-error-formatting helpers for a single import surface."""
    service_uuids = service_uuids or []
    manufacturer_data = manufacturer_data or {}

    if SERVICE_UUID in {u.lower() for u in service_uuids}:
        return True

    payload = manufacturer_data.get(ADV_COMPANY_ID)
    if payload and payload[0] == SUBDEV_TYPE:
        return True

    if name and any(name.upper().startswith(p) for p in NAME_PREFIXES):
        return True

    return False
