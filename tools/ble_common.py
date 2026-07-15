"""
ble_common.py -- shared helpers for the MS605 BLE reverse-engineering toolkit.

Every function in this module is a pure function (no BLE I/O, no asyncio) so it
can be exercised with synthetic inputs from a plain `python -c` call or a
`--self-test` flag, without ever touching a radio or a physical device.

Evidence sources baked into the constants below (see CAPTURE_PLAYBOOK.md /
TOOLKIT_README.md / APK_PROTOCOL.md for the full trail):

- MS605_* (the CONFIRMED MS605 protocol): recovered by static analysis of the
  official Meross Android app (com.meross.meross 3.39.1), decompiled sources
  in apk/j5/. The MS605 uses a SECOND, separate BLE stack (com.meross.ble2 /
  Ble2ConnManager) -- NOT the legacy A00A/JSON one. Its GATT service is
  99E7BE30-0001-4C6B-98A2-70FCB3471A72 (write -0002, notify -0003), and its
  frame is a BINARY TLV envelope:
      55AA | subdevType(1) | length(2 BE) | triggerSrc(1) | msgId(1)
           | TLV[tag(1)|len(2 BE)|value]... | CRC16-CCITT-FALSE(2 BE) | AA55
  subdevType = 0xC0 for MS605; triggerSrc = 0x11 on the wire; msgId rolls
  1..255 (0 == unsolicited push). CRC16 is computed over [triggerSrc..last
  attr byte]. No signing/MD5 on the BLE path (CRC16 integrity only). Every
  example frame below was reproduced byte-for-byte by an independent
  reimplementation of the app's serializer + CRC (see MS605 self-test).
  Sensitivity = tag 61 (1=LOW/2=MED/3=HIGH/4=CUSTOM); detect mode /
  space-learning = tag 52 (1=RADAR/2=RADAR+PIR/3=PIR+RADAR/4=SPACE_LEARNING,
  i.e. auto-calibration); space-learning result pushed on notify tag 62.
- MEROSS_LEGACY_* / packet framing: the OLDER stack (com.meross.ble /
  Ble1ConnManager), used only by Meross Wi-Fi switches/plugs
  (MSS.../MSL.../MRS...) -- confirmed NOT used by the MS605. Service 0000A00A,
  write char 0000B002, notify char 0000B003, packet = 55AA + BE16 length +
  ASCII JSON + BE32 CRC32(json) + AA55. Kept here only for annotating/decoding
  legacy captures, and cross-checked against the open-source Fabi019/MerossBLE
  project. NOTE both stacks share the 55AA.../...AA55 magic bytes but differ in
  everything between them (JSON+CRC32 vs. TLV+CRC16).
- MATTER_CHIPOBLE_*: the standard Matter/CHIP-over-BLE (BTP) commissioning
  transport (service 0xFFF6, characteristics C1/C2/C3). The MS605 is a
  Matter-over-Thread device and also exposes this service for the one-time
  Matter fabric join -- but sensitivity/calibration ride the MS605_* TLV
  service above, NOT Matter clusters or CHIPoBLE.
- Meross has no Bluetooth SIG-registered company identifier (checked against
  the official Bluetooth SIG assigned-numbers list; no "Meross" entry exists).
  Per the decompiled scanner (BleNameSupportUtils.f), the MS605 advertises
  manufacturer_data under company id 0xFFFF (65535) whose short-form TLV
  device-type byte is 0xC0 -- this is the STRONG advertisement signal (plus
  local-name prefixes RFBL_ / MRBL_). Chipset-vendor company IDs (e.g. Nordic
  0x0059) remain a weak, non-authoritative fallback signal.
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Known UUIDs
# --------------------------------------------------------------------------

# CONFIRMED MS605 BLE-config profile (com.meross.ble2 / Ble2ConnManager,
# literal ctor args in apk/j5/.../Ble2ConnManager.java:14).
MS605_SERVICE_UUID = "99e7be30-0001-4c6b-98a2-70fcb3471a72"
MS605_WRITE_CHAR_UUID = "99e7be30-0002-4c6b-98a2-70fcb3471a72"
MS605_NOTIFY_CHAR_UUID = "99e7be30-0003-4c6b-98a2-70fcb3471a72"
# Device-type byte ("subdevType") identifying the MS605 in both the TLV
# command frame and the advertisement short-form TLV (BleNameSupportUtils
# maps byte 0xC0 <-> "ms605").
MS605_SUBDEV_TYPE = 0xC0
# On-wire triggerSrc byte stamped by Ble2ConnManager.f0() just before send.
MS605_TRIGGER_SRC = 0x11
# Advertisement manufacturer-data company id under which the MS605 broadcasts.
MS605_ADV_COMPANY_ID = 0xFFFF
# BLE local-name prefixes used by the MS605 generation (BleNameSupportUtils.c).
MS605_NAME_PREFIXES = ("RFBL_", "MRBL_")

# Legacy Meross BLE Wi-Fi-provisioning profile (com.meross.ble / Ble1; used by
# Meross Wi-Fi switches/plugs -- confirmed NOT used by the MS605).
MEROSS_LEGACY_SERVICE_UUID = "0000a00a-0000-1000-8000-00805f9b34fb"
MEROSS_LEGACY_WRITE_CHAR_UUID = "0000b002-0000-1000-8000-00805f9b34fb"
MEROSS_LEGACY_NOTIFY_CHAR_UUID = "0000b003-0000-1000-8000-00805f9b34fb"

# Standard GATT Client Characteristic Configuration Descriptor.
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# Matter / CHIP-over-BLE (BTP) commissioning transport (standardized, shared
# by every Matter-commissionable device, not Meross-specific by itself).
MATTER_CHIPOBLE_SERVICE_UUID = "0000fff6-0000-1000-8000-00805f9b34fb"
MATTER_C1_WRITE_UUID = "18ee2ef5-263d-4559-959f-4f9c429f9d11"
MATTER_C2_INDICATE_UUID = "18ee2ef5-263d-4559-959f-4f9c429f9d12"
# C3 UUID confirmed against the primary source (connectedhomeip's
# src/ble/BleUUID.h, CHIP_BLE_CHAR_3_UUID_STR) -- note this is NOT in the same
# 18ee2ef5-...-9d1x family as C1/C2.
MATTER_C3_READ_UUID = "64630238-8772-45f2-b87d-748a83218f04"

NOTIFY_LIKE_PROPS = {"notify", "indicate"}
READABLE_PROPS = {"read"}
WRITABLE_PROPS = {"write", "write-without-response"}

# Small, curated, NON-exhaustive subset of the Bluetooth SIG company
# identifiers list (cross-checked live against the SIG's published assigned
# numbers). Used only to annotate manufacturer_data for human readability --
# never treated as a strong Meross signal since Meross has no ID of its own.
KNOWN_COMPANY_IDS = {
    0x004C: "Apple, Inc.",
    0x0006: "Microsoft",
    0x0059: "Nordic Semiconductor ASA",
    0x0075: "Samsung Electronics Co. Ltd.",
    0x00E0: "Google",
    0x02E5: "Espressif Systems (Shanghai) Co., Ltd.",
    0x0157: "Anhui Huami Information Technology Co., Ltd.",
    0x038F: "Xiaomi Inc.",
    0xFFFF: "0xFFFF (reserved/unregistered -- used by the MS605 advertisement)",
}

# Chipset vendor company IDs plausible for Meross devices (weak signal only).
_WEAK_VENDOR_HINT_IDS = {0x0059: "Nordic (BLE/Thread SoC often used by Meross Matter devices)",
                         0x02E5: "Espressif (chipset used by many older Meross Wi-Fi devices)"}

_NAME_PATTERN = re.compile(r"meross|\bms6\d{2}\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# Hex / ASCII helpers
# --------------------------------------------------------------------------

def to_hex(data: bytes) -> str:
    """Lowercase, unseparated hex string, e.g. b'\\xAA\\x01' -> 'aa01'."""
    return data.hex()


def to_hex_spaced(data: bytes) -> str:
    """Space-separated hex bytes, e.g. b'\\xAA\\x01' -> 'aa 01'."""
    return " ".join(f"{b:02x}" for b in data)


def to_ascii(data: bytes) -> str:
    """Best-effort ASCII rendering; non-printable bytes become '.'."""
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)


class HexParseError(ValueError):
    pass


def parse_hex_arg(text: str) -> bytes:
    """Parse a user-supplied hex string into bytes.

    Accepts (and is forgiving of) an optional leading '0x', whitespace, and
    ':' or '-' separators, e.g. "55aa01", "55 AA 01", "0x55aa01", "55:aa:01".
    """
    cleaned = text.strip()
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    cleaned = re.sub(r"[\s:_-]+", "", cleaned)
    if not cleaned:
        raise HexParseError("empty hex payload")
    if len(cleaned) % 2 != 0:
        raise HexParseError(f"odd number of hex digits ({len(cleaned)}) in {text!r}")
    if not re.fullmatch(r"[0-9a-fA-F]+", cleaned):
        raise HexParseError(f"non-hex characters in {text!r}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise HexParseError(str(exc)) from exc


# --------------------------------------------------------------------------
# Meross-candidate heuristic (pure, synthetically testable)
# --------------------------------------------------------------------------

def adv_mentions_ms605_subdev(manufacturer_data: dict[int, bytes] | None) -> bool:
    """True if the advertisement carries MS605 manufacturer data: company id
    0xFFFF whose short-form-TLV device-type byte (first byte) is 0xC0. Mirrors
    BleNameSupportUtils.f() (minus the CRC8 tail check, which we don't require
    for a heuristic match). Never raises."""
    manufacturer_data = manufacturer_data or {}
    payload = manufacturer_data.get(MS605_ADV_COMPANY_ID)
    if not payload:
        return False
    return payload[0] == MS605_SUBDEV_TYPE


def is_meross_candidate(
    name: str | None,
    service_uuids: list[str] | None,
    manufacturer_data: dict[int, bytes] | None,
) -> list[str]:
    """Return a list of human-readable reasons this advertisement looks like
    a Meross device (MS605 signals flagged as strong and clearly separated
    from legacy Wi-Fi-device signals), or an empty list if none apply. Never
    raises."""
    reasons: list[str] = []
    service_uuids = service_uuids or []
    manufacturer_data = manufacturer_data or {}
    lowered_uuids = {u.lower() for u in service_uuids}

    # --- Strong, CONFIRMED MS605 signals (com.meross.ble2) ---
    if MS605_SERVICE_UUID in lowered_uuids:
        reasons.append(
            "advertises the confirmed MS605 BLE-config service 99E7BE30-0001 "
            "(STRONG: this is the MS605's own com.meross.ble2 GATT service)"
        )
    if adv_mentions_ms605_subdev(manufacturer_data):
        reasons.append(
            f"manufacturer_data company id 0x{MS605_ADV_COMPANY_ID:04x} with device-type "
            f"byte 0x{MS605_SUBDEV_TYPE:02x} (STRONG: exactly the MS605 advertisement "
            "signature per BleNameSupportUtils.f)"
        )
    if name and any(name.upper().startswith(p) for p in MS605_NAME_PREFIXES):
        reasons.append(
            f"advertised name {name!r} starts with an MS605-generation prefix "
            f"({'/'.join(MS605_NAME_PREFIXES)}) (STRONG)"
        )

    # --- Weaker / generic Meross-family signals ---
    if name and _NAME_PATTERN.search(name):
        reasons.append(f"advertised name {name!r} matches Meross/MS6xx naming pattern")
    if MEROSS_LEGACY_SERVICE_UUID in lowered_uuids:
        reasons.append(
            "advertises the LEGACY Meross BLE-config service 0000A00A "
            "(this is the Wi-Fi-switch/plug stack -- confirmed NOT the MS605's "
            "protocol; flags a different Meross product line)"
        )
    if MATTER_CHIPOBLE_SERVICE_UUID in lowered_uuids:
        reasons.append(
            "advertises Matter/CHIPoBLE commissioning service 0000FFF6 "
            "(weak signal: shared by ALL Matter-commissionable devices, "
            "but consistent with the MS605 being Matter-over-Thread)"
        )
    for company_id in manufacturer_data:
        if company_id in _WEAK_VENDOR_HINT_IDS:
            reasons.append(
                f"manufacturer_data company id 0x{company_id:04x} "
                f"({_WEAK_VENDOR_HINT_IDS[company_id]}) -- weak signal only, "
                "Meross has no Bluetooth SIG company ID of its own"
            )

    return reasons


def describe_company_id(company_id: int) -> str:
    return KNOWN_COMPANY_IDS.get(company_id, "unknown / not in curated list")


# --------------------------------------------------------------------------
# Shared frame magic. BOTH the MS605 (TLV+CRC16) and the legacy Meross
# (JSON+CRC32) stacks bracket their frames with these same two magic words --
# they differ only in the bytes between them.
# --------------------------------------------------------------------------

MAGIC_HEAD = b"\x55\xaa"
MAGIC_TAIL = b"\xaa\x55"


# --------------------------------------------------------------------------
# MS605 (com.meross.ble2) TLV protocol -- CONFIRMED from app decompilation.
# Frame: 55AA | subdevType(1) | len(2 BE) | triggerSrc(1) | msgId(1)
#             | TLV[tag(1)|len(2 BE)|value]... | CRC16(2 BE) | AA55
# len covers [triggerSrc .. last attr byte]; CRC16 is computed over the same
# range. Mirrors TLVUtils.j()/d() in apk/j5/.../ble2/TLVUtils.java.
# --------------------------------------------------------------------------

# --- MS605 TLV tag numbers (subset that matters for the target operations) ---
MS605_TAG_READ_REQUEST = 2       # value = the tag to read (one per requested tag)
MS605_TAG_STATUS = 3             # response status/error code (0 == success)
MS605_TAG_ZONE_ENABLE = 50       # 0x32 -- 1-byte 7-bit zone-enable bitmask
MS605_TAG_ZONE_THRESHOLDS = 51   # 0x33 -- 28 bytes, 7 zones x [trigger(2 BE)][maintain(2 BE)]
MS605_TAG_DETECT_MODE = 52       # 0x34 -- 1..4; 4 == SPACE_LEARNING (auto-calibration)
MS605_TAG_ZONE_DISTANCE = 53     # 0x35 -- zone distance boundaries (0.1 m units), read
MS605_TAG_SENSITIVITY = 61       # 0x3D -- radar sensitivity 1..4
MS605_TAG_SPACE_LEARNING_RESULT = 62  # 0x3E -- push, value byte 1 == success

# --- MS605 enum values ---
MS605_SENSITIVITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CUSTOM": 4}       # tag 61 value
MS605_DETECT_MODE = {"RADAR": 1, "RADAR_WITH_PIR": 2,
                     "PIR_WITH_RADAR": 3, "SPACE_LEARNING": 4}             # tag 52 value

# Human-readable tag names for decoding/annotation.
MS605_TAG_NAMES = {
    1: "system sub-command", 2: "READ request (value=tag)", 3: "status/error",
    26: "userId (caesar-encrypted)", 30: "id (hex)", 32: "sensor enable",
    33: "time sync", 36: "ambient light", 41: "sub-sensor enable bitmask",
    48: "sub-sensor segment map", 49: "sub-sensor presence/absence times",
    50: "zone enable bitmask", 51: "zone thresholds (28B)", 52: "detect mode / space-learning",
    53: "zone distances (0.1m)", 54: "byte config", 55: "live radar output (push)",
    56: "PIR state", 58: "presence history", 60: "light history",
    61: "radar sensitivity", 62: "space-learning result (push)", 64: "sub-sensor status",
}


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no xorout).
    Byte-for-byte reimplementation of the app's TLVUtils.d()."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def crc8_meross_adv(data: bytes) -> int:
    """CRC-8 (poly 0x07, init 0x00) used only by the MS605 *advertisement*
    short-form TLV. Reimplements the app's TLVUtils.e()."""
    crc = 0
    for byte in data:
        crc ^= (byte & 0xFF)
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc & 0xFF


def build_ms605_tlv_frame(attributes, msg_id: int = 1,
                          subdev_type: int = MS605_SUBDEV_TYPE,
                          trigger_src: int = MS605_TRIGGER_SRC) -> bytes:
    """Build one MS605 TLV command frame.

    attributes: iterable of (tag, value_bytes) pairs.
    Reproduces the app's TLVUtils.j() serializer exactly (verified byte-for-byte
    against the decompiled app's own example frames in the MS605 self-test)."""
    if not 0 <= subdev_type <= 0xFF:
        raise FrameError(f"subdev_type out of byte range: {subdev_type}")
    if not 0 <= msg_id <= 0xFF:
        raise FrameError(f"msg_id out of byte range: {msg_id}")
    if not 0 <= trigger_src <= 0xFF:
        raise FrameError(f"trigger_src out of byte range: {trigger_src}")

    body = bytearray([trigger_src & 0xFF, msg_id & 0xFF])
    for tag, value in attributes:
        value = bytes(value)
        if not 0 <= tag <= 0xFF:
            raise FrameError(f"tag out of byte range: {tag}")
        if len(value) > 0xFFFF:
            raise FrameError(f"TLV value too large for 16-bit length: {len(value)} bytes")
        body += bytes([tag]) + struct.pack(">H", len(value)) + value

    length = len(body)  # == triggerSrc + msgId + all attributes
    crc = crc16_ccitt_false(bytes(body))
    return (MAGIC_HEAD + bytes([subdev_type & 0xFF]) + struct.pack(">H", length)
            + bytes(body) + struct.pack(">H", crc) + MAGIC_TAIL)


@dataclass
class ParsedMS605Frame:
    subdev_type: int
    declared_length: int
    trigger_src: int
    msg_id: int
    attributes: list  # list of (tag, value_bytes)
    crc_ok: bool
    expected_crc: int
    actual_crc: int
    is_push: bool  # msg_id == 0 -> unsolicited push/notification


def parse_ms605_tlv_frame(packet: bytes) -> ParsedMS605Frame:
    """Inverse of build_ms605_tlv_frame(); mirrors TLVUtils.n()/p()/m(...,true).
    Raises FrameError on structural problems (bad magic, truncation, bad TLV
    lengths) but reports CRC mismatch via .crc_ok rather than raising."""
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

    attributes: list = []
    offset = 2
    while offset < len(body):
        if offset + 3 > len(body):
            raise FrameError(f"truncated TLV header at body offset {offset}")
        tag = body[offset]
        vlen = struct.unpack(">H", body[offset + 1:offset + 3])[0]
        value_start = offset + 3
        value_end = value_start + vlen
        if value_end > len(body):
            raise FrameError(
                f"TLV tag {tag} declares {vlen}-byte value but only "
                f"{len(body) - value_start} byte(s) remain"
            )
        attributes.append((tag, body[value_start:value_end]))
        offset = value_end

    actual_crc = struct.unpack(">H", packet[body_end:crc_end])[0]
    expected_crc = crc16_ccitt_false(body)
    return ParsedMS605Frame(
        subdev_type=subdev_type,
        declared_length=declared_length,
        trigger_src=trigger_src,
        msg_id=msg_id,
        attributes=attributes,
        crc_ok=(actual_crc == expected_crc),
        expected_crc=expected_crc,
        actual_crc=actual_crc,
        is_push=(msg_id == 0),
    )


def describe_ms605_frame(parsed: ParsedMS605Frame) -> str:
    """One-line human summary of a parsed MS605 frame, for logs/timelines."""
    kind = "PUSH" if parsed.is_push else f"msgId={parsed.msg_id}"
    parts = []
    for tag, value in parsed.attributes:
        name = MS605_TAG_NAMES.get(tag, f"tag{tag}")
        parts.append(f"{tag}({name})={to_hex(bytes(value))}")
    crc = "crc_ok" if parsed.crc_ok else f"CRC_BAD(exp {parsed.expected_crc:04x})"
    return (f"MS605 subdev=0x{parsed.subdev_type:02x} {kind} trig=0x{parsed.trigger_src:02x} "
            f"[{', '.join(parts) or 'no attrs'}] {crc}")


# --------------------------------------------------------------------------
# Meross legacy packet framing (55AA len JSON crc32 AA55) -- the Wi-Fi-device
# stack (NOT the MS605). Kept for decoding legacy captures and for replay.py's
# optional --meross-envelope mode.
# --------------------------------------------------------------------------


def crc32_be(data: bytes) -> bytes:
    """Standard (zlib/ISO-HDLC) CRC32 over `data`, packed big-endian -- this
    is the same algorithm java.util.zip.CRC32 uses, matching the decompiled
    Meross app's `int2bytes(crc32(data).toInt())`."""
    return struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)


class FrameError(ValueError):
    pass


def build_meross_legacy_packet(payload: bytes) -> bytes:
    """Wrap `payload` (typically ASCII JSON) in the legacy Meross BLE packet
    envelope: 55AA + BE16(len(payload)) + payload + BE32(crc32(payload)) + AA55.
    """
    if len(payload) > 0xFFFF:
        raise FrameError(f"payload too large for 16-bit length field: {len(payload)} bytes")
    return MAGIC_HEAD + struct.pack(">H", len(payload)) + payload + crc32_be(payload) + MAGIC_TAIL


@dataclass
class ParsedMerossLegacyPacket:
    payload: bytes
    declared_length: int
    crc_ok: bool
    expected_crc: bytes
    actual_crc: bytes


def parse_meross_legacy_packet(packet: bytes) -> ParsedMerossLegacyPacket:
    """Inverse of build_meross_legacy_packet(); raises FrameError on
    structural problems (bad magic, truncation) but NOT on CRC mismatch
    (reported via .crc_ok instead, since a mismatch is diagnostic information
    during reverse engineering, not necessarily a fatal parse error)."""
    if len(packet) < 2 + 2 + 4 + 2:
        raise FrameError(f"packet too short ({len(packet)} bytes) to contain a full frame")
    if packet[0:2] != MAGIC_HEAD:
        raise FrameError(f"bad head magic: {packet[0:2].hex()} (expected 55aa)")
    if packet[-2:] != MAGIC_TAIL:
        raise FrameError(f"bad tail magic: {packet[-2:].hex()} (expected aa55)")

    declared_length = struct.unpack(">H", packet[2:4])[0]
    payload_start = 4
    payload_end = payload_start + declared_length
    crc_end = payload_end + 4
    if crc_end + 2 != len(packet):
        raise FrameError(
            f"length mismatch: header declares {declared_length}-byte payload, "
            f"but packet size implies {len(packet) - 2 - 2 - 4 - 2} bytes"
        )

    payload = packet[payload_start:payload_end]
    actual_crc = packet[payload_end:crc_end]
    expected_crc = crc32_be(payload)
    return ParsedMerossLegacyPacket(
        payload=payload,
        declared_length=declared_length,
        crc_ok=(actual_crc == expected_crc),
        expected_crc=expected_crc,
        actual_crc=actual_crc,
    )


# --------------------------------------------------------------------------
# Chunking / per-chunk framing for BLE writes larger than the negotiated MTU
# --------------------------------------------------------------------------

class ChunkFrameError(ValueError):
    pass


def chunk_bytes(data: bytes, chunk_size: int, frame: str = "none") -> list[bytes]:
    """Split `data` into a list of on-air chunks no larger than `chunk_size`
    bytes (after any framing overhead is added).

    frame:
      - "none": raw split, no per-chunk header (matches the Meross app's own
        `splitIntoChunks`: it relies on the *inner* 55AA/AA55 envelope for
        reassembly, not a per-chunk header).
      - "seq":  each chunk is prefixed with a 1-byte sequence number (mod 256).
      - "len":  each chunk is prefixed with a 2-byte big-endian length of the
        chunk's own payload (excluding the 2-byte header itself).
    """
    if chunk_size <= 0:
        raise ChunkFrameError(f"chunk_size must be positive, got {chunk_size}")
    if frame not in ("none", "seq", "len"):
        raise ChunkFrameError(f"unknown frame mode {frame!r} (expected none/seq/len)")

    overhead = {"none": 0, "seq": 1, "len": 2}[frame]
    max_payload = chunk_size - overhead
    if max_payload <= 0:
        raise ChunkFrameError(
            f"chunk_size {chunk_size} too small for frame mode {frame!r} "
            f"(needs > {overhead} bytes of overhead)"
        )

    if not data:
        return []

    chunks: list[bytes] = []
    seq = 0
    for offset in range(0, len(data), max_payload):
        piece = data[offset:offset + max_payload]
        if frame == "none":
            chunks.append(piece)
        elif frame == "seq":
            chunks.append(bytes([seq & 0xFF]) + piece)
        else:  # "len"
            chunks.append(struct.pack(">H", len(piece)) + piece)
        seq += 1
    return chunks


def reassemble_chunks(chunks: list[bytes], frame: str = "none") -> bytes:
    """Inverse of chunk_bytes() for "seq"/"len" framing (strips the header
    bytes back off); for "none" it is simply a concatenation. Sequence
    numbers are validated for "seq" (raises ChunkFrameError on a gap)."""
    if frame not in ("none", "seq", "len"):
        raise ChunkFrameError(f"unknown frame mode {frame!r} (expected none/seq/len)")

    if frame == "none":
        return b"".join(chunks)

    out = bytearray()
    expected_seq = 0
    for chunk in chunks:
        if frame == "seq":
            if len(chunk) < 1:
                raise ChunkFrameError("seq-framed chunk missing 1-byte sequence header")
            seq, piece = chunk[0], chunk[1:]
            if seq != (expected_seq & 0xFF):
                raise ChunkFrameError(f"sequence gap: expected {expected_seq & 0xFF}, got {seq}")
            out += piece
            expected_seq += 1
        else:  # "len"
            if len(chunk) < 2:
                raise ChunkFrameError("len-framed chunk missing 2-byte length header")
            declared = struct.unpack(">H", chunk[0:2])[0]
            piece = chunk[2:]
            if len(piece) != declared:
                raise ChunkFrameError(
                    f"len-framed chunk declares {declared} bytes, got {len(piece)}"
                )
            out += piece
    return bytes(out)


# --------------------------------------------------------------------------
# Adapter / connection error friendliness
# --------------------------------------------------------------------------

def friendly_ble_error(exc: BaseException, address: str | None = None) -> str:
    """Turn a raw bleak/dbus exception into an actionable message. Pure
    string logic -- takes the exception object but does no I/O itself."""
    text = str(exc)
    lowered = text.lower()
    hints: list[str] = []

    if "org.bluez" in lowered and "notready" in lowered.replace(" ", ""):
        hints.append("The Bluetooth adapter is not powered on (try: bluetoothctl power on).")
    if "org.bluez.error.failed" in lowered and "software caused connection abort" in lowered:
        hints.append(
            "The peer likely closed/refused the connection. For the MS605, the device "
            "only accepts GATT connections for a short window after its physical button "
            "is pressed -- ask the device owner to press it again immediately before retrying."
        )
    if "device or resource busy" in lowered or "resource busy" in lowered:
        hints.append("Another process (BlueZ cache, another script) may be holding the connection.")
    if "no such device" in lowered or "does not exist" in lowered:
        hints.append(
            f"No BLE device found matching {address!r}." if address else "No BLE device found."
        )
        hints.append("Confirm the address with scan.py and that the target is currently advertising.")
    if "timed out" in lowered or "timeout" in lowered:
        hints.append(
            "Connection attempt timed out. The MS605 (and similar Meross BLE-config "
            "devices) only accept connections briefly after a physical button press -- "
            "confirm the owner has just triggered pairing/config mode."
        )
    if "permission denied" in lowered:
        hints.append(
            "Permission denied talking to BlueZ. Check your user is in the 'bluetooth' "
            "group or that the adapter/D-Bus policy allows this process access."
        )
    if not hints:
        hints.append("No specific hint matched; see the raw error above for detail.")

    return f"{text}\n  -> " + "\n  -> ".join(hints)


def adapter_status_summary() -> str:
    """Best-effort, dependency-free summary of local BLE adapters for error
    messages. Returns a short string; never raises (falls back to a note if
    /sys is unavailable, e.g. on non-Linux platforms)."""
    import os

    bt_class_dir = "/sys/class/bluetooth"
    try:
        adapters = sorted(os.listdir(bt_class_dir))
    except OSError:
        return "(could not enumerate /sys/class/bluetooth -- not on Linux/BlueZ?)"
    if not adapters:
        return "no Bluetooth adapters found under /sys/class/bluetooth"
    return f"adapters present: {', '.join(adapters)}"
