#!/usr/bin/env python3
"""
btsnoop_att.py -- pure-Python parser for an Android `btsnoop_hci.log` file
(no tshark/Wireshark dependency).

Parses the btsnoop file header + record headers, decodes each HCI ACL data
packet through L2CAP to ATT, reassembles L2CAP fragments and (heuristically)
app-level chunked payloads, and extracts ATT opcodes relevant to GATT
control-channel reverse engineering:

  0x12  Write Request
  0x52  Write Command
  0x16  Prepare Write Request
  0x1b  Handle Value Notification
  0x1d  Handle Value Indication

Prints a chronological timeline and dumps the same data as JSON.

Usage:
    venv/bin/python scripts/btsnoop_att.py --help
    venv/bin/python scripts/btsnoop_att.py captures/btsnoop_hci.log
    venv/bin/python scripts/btsnoop_att.py captures/btsnoop_hci.log --json captures/att_events.json
    venv/bin/python scripts/btsnoop_att.py --self-test

References for the on-disk/wire formats (no external library used):
  - btsnoop file format: https://fte.com/webhelpii/hcidump/appendix/appendix_a.htm
    (16-byte global header "btsnoop\\0" + version + datalink type; then a
    sequence of 24-byte record headers + payload)
  - HCI ACL Data Packet / L2CAP framing / ATT PDU opcodes: Bluetooth Core
    Specification, Vol 4 Part E (HCI) and Vol 3 Part F (ATT).
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ble_common as bc

# --------------------------------------------------------------------------
# btsnoop file-level constants
# --------------------------------------------------------------------------

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_HEADER_LEN = 16  # 8 magic + 4 version + 4 datalink type
BTSNOOP_RECORD_HEADER_LEN = 24  # 4 orig_len + 4 incl_len + 4 flags + 4 drops + 8 ts

# btsnoop record flags bit 0: 0 = sent, 1 = received (direction)
# btsnoop record flags bit 1: 0 = data, 1 = command/event (for HCI datalink)
FLAG_DIRECTION_RECEIVED = 0x01

# HCI packet type prefix byte (only present for datalink type 1002,
# "H1 -- HCI UART (H4)"; datalink type 1001 has no framing byte and is
# raw HCI, which is what most Android btsnoop_hci.log files use).
HCI_ACL_DATA_PACKET = 0x02

# L2CAP fixed channel IDs relevant to ATT
L2CAP_CID_ATT = 0x0004

# ATT opcodes we care about (see module docstring)
ATT_OPCODE_NAMES = {
    0x12: "Write Request",
    0x13: "Write Response",
    0x52: "Write Command",
    0x16: "Prepare Write Request",
    0x17: "Prepare Write Response",
    0x18: "Execute Write Request",
    0x19: "Execute Write Response",
    0x1b: "Handle Value Notification",
    0x1d: "Handle Value Indication",
    0x1e: "Handle Value Confirmation",
    0x0b: "Read Response",
    0x0a: "Read Request",
}
WRITE_LIKE_OPCODES = {0x12, 0x52, 0x16}
NOTIFY_LIKE_OPCODES = {0x1b, 0x1d}
INTERESTING_OPCODES = WRITE_LIKE_OPCODES | NOTIFY_LIKE_OPCODES


class BtsnoopParseError(ValueError):
    pass


@dataclass
class AttEvent:
    record_index: int
    timestamp_str: str
    direction: str  # "sent" | "received"
    opcode: int
    opcode_name: str
    handle: int | None
    value_hex: str
    value_ascii: str
    l2cap_fragmented: bool = False


@dataclass
class ParseResult:
    total_records: int
    acl_records: int
    att_events: list[AttEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# btsnoop timestamp: microseconds since 0000-01-01 00:00:00 UTC ("Symbian
# time"). To get Unix-epoch microseconds: unix_us = raw_ts_us - DELTA.
# DELTA value and subtraction direction cross-checked directly against
# Wireshark's own reference parser (wiretap/btsnoop.c, `KUnixTimeBase`,
# `ts = GINT64_FROM_BE(hdr.ts_usec); ts -= KUnixTimeBase;`) rather than
# derived from a possibly-misremembered calendar calculation.
# --------------------------------------------------------------------------
_BTSNOOP_EPOCH_DELTA_US = 0x00dcddb30f2f8000


def btsnoop_ts_to_str(raw_ts_us: int) -> str:
    """Convert a raw btsnoop 64-bit timestamp to an ISO-ish UTC string.
    Falls back to a raw-value string if the value is out of range (e.g. in a
    hand-built synthetic/malformed record)."""
    import datetime

    unix_us = raw_ts_us - _BTSNOOP_EPOCH_DELTA_US
    try:
        dt = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
            microseconds=unix_us
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"
    except (OverflowError, OSError, ValueError):
        return f"<raw_ts={raw_ts_us}>"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_btsnoop_header(data: bytes) -> int:
    """Validate the 16-byte global header and return the datalink type."""
    if len(data) < BTSNOOP_HEADER_LEN:
        raise BtsnoopParseError(f"file too short for btsnoop header ({len(data)} bytes)")
    magic = data[0:8]
    if magic != BTSNOOP_MAGIC:
        raise BtsnoopParseError(f"bad btsnoop magic: {magic!r} (expected {BTSNOOP_MAGIC!r})")
    version, datalink = struct.unpack(">II", data[8:16])
    if version != 1:
        raise BtsnoopParseError(f"unsupported btsnoop version {version} (expected 1)")
    return datalink


def iter_btsnoop_records(data: bytes):
    """Yield (incl_len, flags, ts_str, payload_bytes) tuples for every record
    in the body (after the 16-byte global header)."""
    offset = BTSNOOP_HEADER_LEN
    n = len(data)
    while offset < n:
        if offset + BTSNOOP_RECORD_HEADER_LEN > n:
            raise BtsnoopParseError(
                f"truncated record header at offset {offset} ({n - offset} bytes left)"
            )
        orig_len, incl_len, flags, drops, ts_raw = struct.unpack(
            ">iiiiq", data[offset:offset + BTSNOOP_RECORD_HEADER_LEN]
        )
        payload_start = offset + BTSNOOP_RECORD_HEADER_LEN
        payload_end = payload_start + incl_len
        if incl_len < 0 or payload_end > n:
            raise BtsnoopParseError(
                f"record at offset {offset} declares incl_len={incl_len}, "
                f"but only {n - payload_start} byte(s) remain"
            )
        payload = data[payload_start:payload_end]
        yield incl_len, flags, ts_raw, payload
        offset = payload_end


def _parse_acl_l2cap_att(hci_payload: bytes):
    """Given the bytes of one HCI ACL Data Packet (starting at the HCI packet
    type byte, i.e. including the leading 0x02), return
    (handle, pb_flag, l2cap_len, l2cap_cid, l2cap_payload) or None if this
    isn't a well-formed ACL packet we can parse (e.g. it's some other HCI
    packet type, or a fragment we can't fully decode alone)."""
    if len(hci_payload) < 1:
        return None
    pkt_type = hci_payload[0]
    if pkt_type != HCI_ACL_DATA_PACKET:
        return None
    if len(hci_payload) < 1 + 4:
        return None

    handle_and_flags, data_len = struct.unpack("<HH", hci_payload[1:5])
    handle = handle_and_flags & 0x0FFF
    pb_flag = (handle_and_flags >> 12) & 0x3

    acl_data = hci_payload[5:5 + data_len]
    if len(acl_data) < data_len:
        return None  # truncated capture

    if pb_flag == 0b10 or pb_flag == 0b00:
        # Start of an L2CAP PDU (or complete, non-fragmented, PDU): the first
        # 4 bytes are the L2CAP basic header (length + CID).
        if len(acl_data) < 4:
            return None
        l2cap_len, l2cap_cid = struct.unpack("<HH", acl_data[0:4])
        l2cap_payload = acl_data[4:]
        return handle, "start", l2cap_len, l2cap_cid, l2cap_payload
    else:
        # Continuation fragment: no L2CAP header, just more payload bytes for
        # whatever PDU is already in flight on this connection handle.
        return handle, "cont", None, None, acl_data


def _decode_app_frame(buf: bytes, handle: int, record_index: int) -> str | None:
    """Best-effort app-layer decode of a fully-accumulated 55AA..AA55 frame.
    Both the MS605 (TLV+CRC16) and legacy Meross (JSON+CRC32) stacks share the
    same magic, so try the MS605 TLV parse first (it validates via CRC16 and
    structure) and fall back to the legacy JSON parse. Returns a human-readable
    note, or None if neither parse structurally succeeds. Never raises."""
    # Try MS605 TLV first.
    try:
        ms605 = bc.parse_ms605_tlv_frame(buf)
        if ms605.crc_ok:
            return (f"record {record_index}: MS605 TLV frame on handle 0x{handle:04x} "
                    f"({len(buf)} B) -> {bc.describe_ms605_frame(ms605)}")
    except bc.FrameError:
        ms605 = None
    # Fall back to legacy JSON.
    try:
        legacy = bc.parse_meross_legacy_packet(buf)
        return (f"record {record_index}: legacy Meross JSON frame on handle 0x{handle:04x} "
                f"({len(buf)} B, crc_ok={legacy.crc_ok}): {legacy.payload[:120]!r}")
    except bc.FrameError:
        pass
    # MS605 parse succeeded structurally but CRC failed -> still worth flagging.
    if ms605 is not None:
        return (f"record {record_index}: MS605 TLV frame on handle 0x{handle:04x} "
                f"({len(buf)} B) -> {bc.describe_ms605_frame(ms605)}")
    return None


def parse_att_events(raw: bytes) -> ParseResult:
    datalink = parse_btsnoop_header(raw)
    if datalink not in (1001, 1002):
        # 1001 = "H1 -- HCI in UART/H4 format without the 1-byte type prefix"?
        # In practice Android's btsnoop_hci.log always uses 1002 (H4, WITH the
        # type-prefix byte) or occasionally 1001; we don't hard-fail on other
        # values, just note it, since some vendor captures use nonstandard
        # datalink type values but still contain valid H4-framed records.
        pass

    events: list[AttEvent] = []
    warnings: list[str] = []

    # Per-connection-handle L2CAP reassembly buffers: handle -> (cid, expected_len, bytes_so_far)
    l2cap_reassembly: dict[int, tuple[int, int, bytearray]] = {}
    # Per-connection-handle app-layer (MS605 TLV or legacy Meross JSON, both
    # bracketed by 55AA..AA55) reassembly: handle -> bytearray accumulated
    # since a 55AA start marker was seen. Decoded by _decode_app_frame().
    app_reassembly: dict[int, bytearray] = {}

    total_records = 0
    acl_records = 0

    for record_index, (incl_len, flags, ts_raw, payload) in enumerate(iter_btsnoop_records(raw)):
        total_records += 1
        direction = "received" if (flags & FLAG_DIRECTION_RECEIVED) else "sent"
        ts_str = btsnoop_ts_to_str(ts_raw)

        parsed = _parse_acl_l2cap_att(payload)
        if parsed is None:
            continue
        acl_records += 1
        handle, frag_kind, l2cap_len, l2cap_cid, l2cap_payload = parsed

        if frag_kind == "start":
            if l2cap_cid != L2CAP_CID_ATT:
                # Not the ATT fixed channel (e.g. SMP 0x0006, signaling 0x0005) --
                # not interesting for GATT command reverse engineering.
                l2cap_reassembly.pop(handle, None)
                continue
            if len(l2cap_payload) >= l2cap_len:
                complete_att_pdu = l2cap_payload[:l2cap_len]
                l2cap_reassembly.pop(handle, None)
            else:
                l2cap_reassembly[handle] = (l2cap_cid, l2cap_len, bytearray(l2cap_payload))
                continue
        else:  # "cont"
            pending = l2cap_reassembly.get(handle)
            if pending is None:
                # Continuation with no known start (e.g. capture starts mid-PDU,
                # or it's a non-ATT channel we already discarded) -- skip.
                continue
            cid, expected_len, buf = pending
            buf += l2cap_payload
            if len(buf) < expected_len:
                l2cap_reassembly[handle] = (cid, expected_len, buf)
                continue
            complete_att_pdu = bytes(buf[:expected_len])
            l2cap_reassembly.pop(handle, None)

        if not complete_att_pdu:
            continue
        opcode = complete_att_pdu[0]
        if opcode not in INTERESTING_OPCODES:
            continue

        att_value = complete_att_pdu[1:]
        att_handle = None
        value_bytes = att_value
        if len(att_value) >= 2:
            att_handle = struct.unpack("<H", att_value[0:2])[0]
            value_bytes = att_value[2:]

        event = AttEvent(
            record_index=record_index,
            timestamp_str=ts_str,
            direction=direction,
            opcode=opcode,
            opcode_name=ATT_OPCODE_NAMES.get(opcode, f"unknown(0x{opcode:02x})"),
            handle=att_handle,
            value_hex=bc.to_hex(value_bytes),
            value_ascii=bc.to_ascii(value_bytes),
        )
        events.append(event)

        # App-layer (Meross-style 55AA .. AA55) reassembly heuristic, purely
        # for operator convenience in the printed timeline -- best-effort,
        # never raises.
        if opcode in WRITE_LIKE_OPCODES | NOTIFY_LIKE_OPCODES:
            buf = app_reassembly.get(handle)
            if value_bytes[:2] == bc.MAGIC_HEAD:
                buf = bytearray(value_bytes)
                app_reassembly[handle] = buf
            elif buf is not None:
                buf += value_bytes
            if buf is not None and buf[-2:] == bc.MAGIC_TAIL and len(buf) >= 11:
                note = _decode_app_frame(bytes(buf), handle, record_index)
                if note is not None:
                    event.l2cap_fragmented = True  # reuse field to flag "app-reassembled"
                    warnings.append(note)
                app_reassembly.pop(handle, None)

    return ParseResult(
        total_records=total_records,
        acl_records=acl_records,
        att_events=events,
        warnings=warnings,
    )


def render_timeline(result: ParseResult) -> str:
    lines = [
        f"{result.total_records} btsnoop record(s), {result.acl_records} ACL data record(s), "
        f"{len(result.att_events)} interesting ATT event(s)",
        "",
    ]
    for e in result.att_events:
        handle_str = f"handle=0x{e.handle:04x}" if e.handle is not None else "handle=?"
        lines.append(
            f"[{e.timestamp_str}] {e.direction:8s} {e.opcode_name:28s} {handle_str}  "
            f"hex={e.value_hex}  ascii={e.value_ascii!r}"
        )
    if result.warnings:
        lines.append("")
        lines.append("App-layer reassembly notes:")
        for w in result.warnings:
            lines.append(f"  - {w}")
    if not result.att_events:
        lines.append("(no Write Request/Command/Prepare-Write/Notify/Indicate PDUs found)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Synthetic btsnoop builder, for --self-test and for anyone who wants to
# generate a tiny fixture by hand.
# --------------------------------------------------------------------------

def _build_hci_acl_packet(handle: int, pb_flag: int, l2cap_cid: int | None,
                           l2cap_total_len: int | None, fragment_payload: bytes) -> bytes:
    """Build one HCI ACL Data Packet (H4-framed, i.e. with the leading 0x02
    packet-type byte), optionally including the L2CAP basic header (only on
    the first fragment of a PDU)."""
    if pb_flag == 0b10 or pb_flag == 0b00:
        acl_data = struct.pack("<HH", l2cap_total_len, l2cap_cid) + fragment_payload
    else:
        acl_data = fragment_payload
    handle_and_flags = (handle & 0x0FFF) | ((pb_flag & 0x3) << 12)
    header = struct.pack("<HH", handle_and_flags, len(acl_data))
    return bytes([HCI_ACL_DATA_PACKET]) + header + acl_data


def _build_btsnoop_record(hci_packet: bytes, direction_received: bool, ts_us: int) -> bytes:
    incl_len = orig_len = len(hci_packet)
    flags = FLAG_DIRECTION_RECEIVED if direction_received else 0
    drops = 0
    raw_ts = ts_us + _BTSNOOP_EPOCH_DELTA_US
    header = struct.pack(">iiiiq", orig_len, incl_len, flags, drops, raw_ts)
    return header + hci_packet


def _synthetic_write_frame() -> bytes:
    """The MS605 TLV frame carried by the synthetic capture's Write Command:
    a 28-byte zone-thresholds write (tag 51). Chosen because it is one of the
    three target operations AND is large enough (~42 bytes) to genuinely
    fragment at a 20-byte MTU, exercising L2CAP reassembly."""
    zone_value = b"".join(
        t.to_bytes(2, "big") + m.to_bytes(2, "big")
        for t, m in zip([300, 350, 400, 450, 500, 550, 600],
                        [200, 250, 300, 350, 400, 450, 500])
    )
    return bc.build_ms605_tlv_frame([(bc.MS605_TAG_ZONE_THRESHOLDS, zone_value)], msg_id=1)


def build_synthetic_btsnoop(fragment_size: int = 20) -> bytes:
    """Build a small, in-memory, synthetic btsnoop capture containing:
      1. A Write Command (0x52) carrying an MS605 TLV zone-thresholds frame,
         split across TWO L2CAP/ACL fragments (to exercise reassembly).
      2. A Handle Value Notification (0x1b) carrying an MS605 space-learning
         result push (single fragment).
      3. An uninteresting Read Request (0x0a) that must be ignored.
    """
    out = bytearray()
    out += BTSNOOP_MAGIC
    out += struct.pack(">II", 1, 1002)  # version 1, datalink 1002 (H4)

    framed = _synthetic_write_frame()  # 55aa + subdev + len + trig + msgid + TLV + crc16 + aa55

    # --- Event 1: Write Command carrying `framed`, fragmented in two pieces ---
    att_pdu = bytes([0x52]) + struct.pack("<H", 0x002a) + framed  # opcode + handle + value
    piece1 = att_pdu[:fragment_size]
    piece2 = att_pdu[fragment_size:]

    frag1 = _build_hci_acl_packet(
        handle=0x0040, pb_flag=0b10, l2cap_cid=L2CAP_CID_ATT,
        l2cap_total_len=len(att_pdu), fragment_payload=piece1,
    )
    out += _build_btsnoop_record(frag1, direction_received=False, ts_us=1_000_000)

    frag2 = _build_hci_acl_packet(handle=0x0040, pb_flag=0b01, l2cap_cid=None,
                                    l2cap_total_len=None, fragment_payload=piece2)
    out += _build_btsnoop_record(frag2, direction_received=False, ts_us=1_010_000)

    # --- Event 2: Handle Value Notification = MS605 space-learning result push
    #     (msgId 0 == push, tag 62 value 1 == success), single fragment ---
    notify_frame = bc.build_ms605_tlv_frame(
        [(bc.MS605_TAG_SPACE_LEARNING_RESULT, b"\x01")], msg_id=0
    )
    notify_pdu = bytes([0x1b]) + struct.pack("<H", 0x002d) + notify_frame
    frag3 = _build_hci_acl_packet(
        handle=0x0040, pb_flag=0b10, l2cap_cid=L2CAP_CID_ATT,
        l2cap_total_len=len(notify_pdu), fragment_payload=notify_pdu,
    )
    out += _build_btsnoop_record(frag3, direction_received=True, ts_us=1_020_000)

    # --- Event 3: uninteresting Read Request, must NOT show up in results ---
    read_pdu = bytes([0x0a]) + struct.pack("<H", 0x0030)
    frag4 = _build_hci_acl_packet(
        handle=0x0040, pb_flag=0b10, l2cap_cid=L2CAP_CID_ATT,
        l2cap_total_len=len(read_pdu), fragment_payload=read_pdu,
    )
    out += _build_btsnoop_record(frag4, direction_received=False, ts_us=1_030_000)

    return bytes(out)


def self_test() -> int:
    # Timestamp conversion: raw_ts_us == the epoch delta itself must map to
    # exactly the Unix epoch (1970-01-01T00:00:00.000000Z), and one exact
    # microsecond later must roll over correctly. This is checked against
    # the literal DELTA constant rather than a derived value so a future
    # accidental edit of the constant cannot silently make this tautological.
    assert btsnoop_ts_to_str(_BTSNOOP_EPOCH_DELTA_US) == "1970-01-01T00:00:00.000000Z"
    assert btsnoop_ts_to_str(_BTSNOOP_EPOCH_DELTA_US + 1) == "1970-01-01T00:00:00.000001Z"
    assert btsnoop_ts_to_str(_BTSNOOP_EPOCH_DELTA_US + 86_400_000_000) == "1970-01-02T00:00:00.000000Z"

    synthetic = build_synthetic_btsnoop(fragment_size=20)

    # Header parses and round-trips.
    datalink = parse_btsnoop_header(synthetic)
    assert datalink == 1002

    # Bad magic is rejected.
    try:
        parse_btsnoop_header(b"NOTBTSNOOP" + b"\x00" * 20)
        assert False, "should have raised on bad magic"
    except BtsnoopParseError:
        pass

    # Truncated file is rejected cleanly.
    try:
        parse_btsnoop_header(b"short")
        assert False, "should have raised on short header"
    except BtsnoopParseError:
        pass

    result = parse_att_events(synthetic)
    assert result.total_records == 4, result.total_records
    assert result.acl_records == 4, result.acl_records
    assert len(result.att_events) == 2, [e.opcode_name for e in result.att_events]  # Read Request excluded

    write_event, notify_event = result.att_events
    assert write_event.opcode == 0x52
    assert write_event.opcode_name == "Write Command"
    assert write_event.direction == "sent"
    assert write_event.handle == 0x002a
    # The fragmented Write Command's reassembled value must equal the exact
    # bytes of the MS605 zone-thresholds TLV frame we built it from. (This
    # frame is >20 bytes, so it genuinely spanned two L2CAP fragments.)
    expected_framed = _synthetic_write_frame()
    assert len(expected_framed) > 20, "write frame must exceed one 20-byte fragment"
    assert bytes.fromhex(write_event.value_hex) == expected_framed, (
        write_event.value_hex, expected_framed.hex()
    )

    assert notify_event.opcode == 0x1b
    assert notify_event.opcode_name == "Handle Value Notification"
    assert notify_event.direction == "received"
    assert notify_event.handle == 0x002d
    # The notification is an MS605 space-learning-result push (tag 62, value 1).
    notify_parsed = bc.parse_ms605_tlv_frame(bytes.fromhex(notify_event.value_hex))
    assert notify_parsed.is_push
    assert notify_parsed.attributes == [(bc.MS605_TAG_SPACE_LEARNING_RESULT, b"\x01")]

    # App-layer reassembly heuristic must have decoded the MS605 TLV frame
    # inside the (already L2CAP-reassembled) write event, and named the tag.
    assert any("MS605 TLV frame" in w and "zone thresholds" in w for w in result.warnings), \
        result.warnings
    assert write_event.l2cap_fragmented is True

    timeline = render_timeline(result)
    assert "Write Command" in timeline
    assert "Handle Value Notification" in timeline
    assert "Read Request" not in timeline  # must be filtered out

    # JSON dump round trip.
    payload = {
        "total_records": result.total_records,
        "acl_records": result.acl_records,
        "att_events": [asdict(e) for e in result.att_events],
        "warnings": result.warnings,
    }
    dumped = json.dumps(payload, indent=2)
    reloaded = json.loads(dumped)
    assert reloaded["att_events"][0]["opcode"] == 0x52

    # Also test with a single-fragment (unfragmented) build to make sure the
    # non-fragmented path (fragment_size >= whole PDU) still works.
    synthetic_unfrag = build_synthetic_btsnoop(fragment_size=10_000)
    result2 = parse_att_events(synthetic_unfrag)
    assert len(result2.att_events) == 2
    assert bytes.fromhex(result2.att_events[0].value_hex) == expected_framed

    # Mutation check: corrupt one byte inside the MS605 TLV value and confirm
    # the CRC16 check correctly flips crc_ok to False, proving the assertion is
    # real and not a tautology. (_decode_app_frame still reports the frame,
    # but describe_ms605_frame flags CRC_BAD.)
    tampered = bytearray(expected_framed)
    tampered[10] ^= 0xFF  # flip the sensitivity value byte, leave CRC untouched
    parsed_tampered = bc.parse_ms605_tlv_frame(bytes(tampered))
    assert parsed_tampered.crc_ok is False

    # And the legacy JSON decode path still works as a fallback (a capture from
    # a Wi-Fi Meross device would hit this branch).
    legacy_framed = bc.build_meross_legacy_packet(b'{"header":{},"payload":{}}')
    legacy_note = _decode_app_frame(legacy_framed, 0x0040, 99)
    assert legacy_note is not None and "legacy Meross JSON frame" in legacy_note

    print("ALL btsnoop_att.py SELF-TESTS PASSED")
    print(timeline)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pure-Python parser for an Android btsnoop_hci.log: decodes HCI ACL -> "
                    "L2CAP -> ATT and extracts Write Request/Command, Prepare Write, and "
                    "Handle Value Notification/Indication PDUs. No tshark dependency.",
    )
    parser.add_argument("logfile", nargs="?", default=None,
                         help="path to btsnoop_hci.log")
    parser.add_argument("--json", metavar="PATH", default=None,
                         help="also write the parsed events to this JSON file")
    parser.add_argument("--self-test", action="store_true",
                         help="build a synthetic in-memory btsnoop buffer, parse it, and "
                              "verify extraction; exit without touching any real file")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.logfile:
        parser.error("logfile is required (unless --self-test)")

    path = Path(args.logfile)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return 1

    try:
        result = parse_att_events(raw)
    except BtsnoopParseError as exc:
        print(f"ERROR: failed to parse {path} as a btsnoop file: {exc}", file=sys.stderr)
        return 1

    print(render_timeline(result))

    if args.json:
        payload = {
            "source_file": str(path),
            "total_records": result.total_records,
            "acl_records": result.acl_records,
            "att_events": [asdict(e) for e in result.att_events],
            "warnings": result.warnings,
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {len(result.att_events)} event(s) to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
