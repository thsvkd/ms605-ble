"""Offline protocol checks built from synthetic frames and standard vectors."""

from __future__ import annotations

import struct
from datetime import datetime, timezone

import pytest

from ms605.errors import FrameError
from ms605.protocol import (
    PUSH_TRIGGER_SRC,
    SUBDEV_TYPE,
    TAG_DETECT_MODE,
    TAG_SENSITIVITY,
    TAG_SPACE_LEARNING_RESULT,
    TAG_SYSTEM_SUBCOMMAND,
    TRIGGER_SRC,
    FrameReassembler,
    build_command,
    build_frame_raw,
    chunk_frame,
    crc16_ccitt_false,
    is_ms605_advertisement,
    msg_id_sequence,
    parse_frame,
)

SYNTHETIC_COMMAND_FRAMES = (
    (
        "set sensitivity=4 (CUSTOM), msgId=0x2A",
        [(TAG_SENSITIVITY, bytes([4]))],
        0x2A,
        "55aac0000a112a3d00010401000106abd3aa55",
    ),
    (
        "start auto-calibration (tag52=4), msgId=0x2B",
        [(TAG_DETECT_MODE, bytes([4]))],
        0x2B,
        "55aac0000a112b340001040100010619f9aa55",
    ),
    (
        # bare keep-alive: only the mandatory tag1=06 trailer, no real attr.
        # This is exactly what MS605.ping() emits to keep the link alive.
        "keep-alive (bare tag1=06 trailer), msgId=0x2C",
        [],
        0x2C,
        "55aac00006112c01000106f7e8aa55",
    ),
)

SYNTHETIC_PUSH_FRAME_HEX = "55aac0000600003e000101a3b3aa55"


def test_crc16_standard_check_value():
    assert crc16_ccitt_false(b"123456789") == 0x29B1


@pytest.mark.parametrize("name,attrs,msg_id,expected_hex", SYNTHETIC_COMMAND_FRAMES)
def test_synthetic_command_frames(name, attrs, msg_id, expected_hex):
    assert build_command(attrs, msg_id).hex() == expected_hex, name


def test_synthetic_push_frame_parses_and_rebuilds():
    push_bytes = bytes.fromhex(SYNTHETIC_PUSH_FRAME_HEX)
    parsed = parse_frame(push_bytes)
    assert parsed.crc_ok
    assert parsed.is_push
    assert parsed.get(TAG_SPACE_LEARNING_RESULT) == b"\x01"

    rebuilt = build_frame_raw(
        [(TAG_SPACE_LEARNING_RESULT, b"\x01")], msg_id=0, trigger_src=PUSH_TRIGGER_SRC
    ).hex()
    assert rebuilt == SYNTHETIC_PUSH_FRAME_HEX


def test_parse_round_trip():
    frame = build_command([(TAG_SENSITIVITY, bytes([3]))], msg_id=42)
    parsed = parse_frame(frame)
    assert parsed.msg_id == 42
    assert parsed.trigger_src == TRIGGER_SRC
    assert parsed.subdev_type == SUBDEV_TYPE
    assert parsed.crc_ok
    assert not parsed.is_push
    assert parsed.attributes == [(TAG_SENSITIVITY, b"\x03"), (TAG_SYSTEM_SUBCOMMAND, b"\x06")]


def test_crc_mismatch_is_reported_not_raised():
    frame = bytearray(build_command([(TAG_SENSITIVITY, bytes([3]))], msg_id=42))
    frame[-3] ^= 0xFF  # flip a bit in the low CRC byte
    parsed = parse_frame(bytes(frame))
    assert parsed.crc_ok is False


@pytest.mark.parametrize(
    "packet",
    [
        b"\x00" * 20,  # bad head magic
        bytes.fromhex(SYNTHETIC_COMMAND_FRAMES[0][3])[:-4],  # truncated CRC/tail
        b"\x55\xaa\xc0\x00\x00",  # too short
    ],
)
def test_malformed_frames_raise(packet):
    with pytest.raises(FrameError):
        parse_frame(packet)


def test_chunking_covers_whole_frame_within_size_limit():
    frame = build_command([(TAG_SENSITIVITY, bytes([3]))], msg_id=42)
    chunks = chunk_frame(frame, chunk_size=8)
    assert all(len(c) <= 8 for c in chunks)
    assert b"".join(chunks) == frame


def test_reassembly_reproduces_one_chunked_frame():
    frame = build_command([(TAG_SENSITIVITY, bytes([3]))], msg_id=42)
    chunks = chunk_frame(frame, chunk_size=8)
    reassembler = FrameReassembler()
    out = []
    for c in chunks:
        out.extend(reassembler.feed(c))
    assert out == [frame]


def test_reassembly_handles_two_back_to_back_frames():
    frame_a = build_command([(TAG_SENSITIVITY, bytes([1]))], msg_id=10)
    frame_b = build_command([(TAG_DETECT_MODE, bytes([2]))], msg_id=11)
    combined = frame_a + frame_b
    reassembler = FrameReassembler()
    out = []
    for i in range(0, len(combined), 7):
        out.extend(reassembler.feed(combined[i : i + 7]))
    assert out == [frame_a, frame_b]


def test_msg_id_rolls_and_skips_zero():
    gen = msg_id_sequence(start=254)
    ids = [next(gen) for _ in range(4)]
    assert ids == [254, 255, 1, 2]


@pytest.mark.parametrize(
    "name,service_uuids,manufacturer_data,expected",
    [
        ("configured service UUID", ["99e7be30-0001-4c6b-98a2-70fcb3471a72"], None, True),
        ("manufacturer data 0xFFFF/0xC0", None, {0xFFFF: bytes([0xC0, 0x00])}, True),
        ("name prefix RFBL_", None, None, True),
        ("name prefix MRBL_", None, None, True),
        ("unrelated device", ["0000180f-0000-1000-8000-00805f9b34fb"], {0x004C: b"\x01"}, False),
    ],
)
def test_advertisement_matching(name, service_uuids, manufacturer_data, expected):
    adv_name = {"name prefix RFBL_": "RFBL_1234", "name prefix MRBL_": "MRBL_ABCD"}.get(name)
    if name == "unrelated device":
        adv_name = "SomeOtherThing"
    assert is_ms605_advertisement(adv_name, service_uuids, manufacturer_data) is expected


def test_tag33_time_sync_uses_u32_big_endian():
    """Pin byte order with a synthetic epoch rather than an observed value."""
    epoch = 1_700_000_000
    encoded = struct.pack(">I", epoch)
    assert encoded == bytes.fromhex("6553f100")
    assert struct.unpack(">I", encoded)[0] == epoch
    assert datetime.fromtimestamp(epoch, timezone.utc) == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
    )
