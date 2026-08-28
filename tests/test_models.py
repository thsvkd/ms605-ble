"""Offline checks for decoded value types using synthetic protocol fixtures."""

from __future__ import annotations

import struct

import pytest

from ms605.errors import FrameError
from ms605.models import (
    decode_config,
    decode_dnd,
    decode_light_history,
    decode_pir_state,
    decode_presence_absence_times,
    decode_presence_history,
    decode_sample_intervals,
    decode_segment_map,
    decode_sub_sensor_status,
    decode_supported_tags,
    decode_zone_distances,
    decode_zone_thresholds,
    encode_presence_absence_times,
    encode_sample_interval,
    encode_segment_map,
    encode_subsensor_enable,
    encode_zone_thresholds,
)
from ms605.protocol import (
    TAG_DETECT_MODE,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_SEGMENT_MAP,
    TAG_SENSITIVITY,
    TAG_STATUS,
    TAG_SUBSENSOR_ENABLE,
    TAG_ZONE_DISTANCES,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    build_frame_raw,
    parse_frame,
)

# Protocol-minimal synthetic values. They exercise every decoded config field
# without retaining values observed from a physical sensor.
SYNTHETIC_CONFIG_ATTRS = [
    (TAG_SUBSENSOR_ENABLE, bytes.fromhex("05")),
    (TAG_SEGMENT_MAP, bytes.fromhex("030c7000")),
    (TAG_PRESENCE_ABSENCE_TIMES, bytes.fromhex("0005001e000a002d000f003c00000000")),
    (TAG_ZONE_ENABLE, bytes.fromhex("7f")),
    (TAG_ZONE_THRESHOLDS, bytes.fromhex("005a0028005000230046001e003c0019003200140028000f001e000a")),
    (TAG_DETECT_MODE, bytes.fromhex("02")),
    (TAG_ZONE_DISTANCES, bytes.fromhex("000810182028323c")),
    (TAG_SENSITIVITY, bytes.fromhex("04")),
    (TAG_STATUS, bytes.fromhex("00")),
]

# Synthetic tag-64 payload: mask + four presence epochs + four absence epochs.
SYNTHETIC_SUBSENSOR_STATUS_HEX = (
    "056553f1006553f1c86553f290000000006553f1646553f22c6553f2f400000000"
)


def test_decode_config_from_synthetic_frame():
    frame = parse_frame(build_frame_raw(SYNTHETIC_CONFIG_ATTRS, msg_id=42))
    assert frame.crc_ok
    config = decode_config(frame)
    assert config.sensitivity == 4
    assert config.detect_mode == 2
    assert config.zone_enable == 0x7F and config.zones_enabled() == (True,) * 7
    assert [(t.presence_seconds, t.absence_seconds) for t in config.presence_absence_times] == [
        (5, 30), (10, 45), (15, 60),
    ]
    assert config.sub_sensor_enable == 0x05
    assert [zm.mask for zm in config.segment_map] == [0x03, 0x0C, 0x70]
    assert config.segment_map[0].zones == (0, 1)
    assert config.segment_map[1].zones == (2, 3)

    expected_distances = (0.0, 0.8, 1.6, 2.4, 3.2, 4.0, 5.0, 6.0)
    assert all(abs(a - b) < 1e-9 for a, b in zip(config.zone_distances_m, expected_distances, strict=True))

    expected_thresholds = ((90, 40), (80, 35), (70, 30), (60, 25), (50, 20), (40, 15), (30, 10))
    got = tuple((z.trigger, z.maintain) for z in config.zone_thresholds)
    assert got == expected_thresholds


def test_zone_threshold_encode_decode_round_trip():
    pairs = [(95, 40), (85, 40), (75, 40), (60, 40), (55, 40), (40, 35), (35, 28)]
    encoded = encode_zone_thresholds(pairs)
    assert len(encoded) == 28
    decoded = decode_zone_thresholds(encoded)
    assert [(z.trigger, z.maintain) for z in decoded] == pairs


def test_zone_threshold_encoding_rejects_wrong_count():
    with pytest.raises(ValueError):
        encode_zone_thresholds([(95, 40)] * 6)


def test_zone_distances_decode():
    assert decode_zone_distances(bytes.fromhex("000810182028323c")) == (
        0.0, 0.8, 1.6, 2.4, 3.2, 4.0, 5.0, 6.0,
    )


def test_presence_absence_times_round_trip():
    timings = [(0, 30), (5, 60), (10, 120)]
    encoded = encode_presence_absence_times(timings)
    assert len(encoded) == 16  # 3 real pairs + 4 padding bytes for the unused 4th slot
    decoded = decode_presence_absence_times(encoded)
    assert [(t.presence_seconds, t.absence_seconds) for t in decoded] == timings


def test_encode_presence_absence_times_rejects_wrong_count():
    with pytest.raises(ValueError):
        encode_presence_absence_times([(0, 30), (0, 30)])


def test_segment_map_round_trip():
    zone_lists = [[0, 1, 2, 3, 4, 5, 6], [], [2, 4]]
    encoded = encode_segment_map(zone_lists)
    assert len(encoded) == 4  # 3 real bytes + 1 padding byte for the unused 4th slot
    decoded = decode_segment_map(encoded)
    assert [list(zm.zones) for zm in decoded] == zone_lists
    assert decoded[0].mask == 0x7F
    assert decoded[2].mask == 0b10100  # bits 2 and 4 set


def test_encode_segment_map_rejects_wrong_count():
    with pytest.raises(ValueError):
        encode_segment_map([[0], [1]])


def test_encode_segment_map_rejects_out_of_range_zone():
    with pytest.raises(ValueError):
        encode_segment_map([[0], [], [7]])


def test_encode_subsensor_enable_packs_bitmask():
    assert encode_subsensor_enable([True, True, True]) == 0b111
    assert encode_subsensor_enable([True, False, True]) == 0b101
    assert encode_subsensor_enable([False, False, False]) == 0


def test_decode_sub_sensor_status_from_synthetic_payload():
    value = bytes.fromhex(SYNTHETIC_SUBSENSOR_STATUS_HEX)
    assert len(value) == 33
    statuses = decode_sub_sensor_status(value, sub_sensor_count=3)
    assert len(statuses) == 3
    assert statuses[0].has_presence is True  # mask byte = 0x01, bit 0 set
    assert statuses[1].has_presence is False
    assert statuses[2].has_presence is True
    assert statuses[0].presence_timestamp == 1700000000
    assert statuses[0].absence_timestamp == 1700000100
    assert statuses[1].presence_timestamp == 1700000200
    assert statuses[1].absence_timestamp == 1700000300
    assert statuses[2].presence_timestamp == 1700000400
    assert statuses[2].absence_timestamp == 1700000500


def test_decode_sub_sensor_status_rejects_short_payload():
    with pytest.raises(FrameError):
        decode_sub_sensor_status(b"\x00" * 10, sub_sensor_count=3)


def test_decode_presence_history_simple_synthetic_round_trip():
    # 9-byte record: index(u16) sensor_mask(1) zone_enable(1) zone_presence(1) timestamp(u32)
    raw = struct.pack(">H", 7) + bytes([0b101, 0x7F, 0x03]) + struct.pack(">I", 1700000000)
    records = decode_presence_history(raw, detail=False)
    assert len(records) == 1
    rec = records[0]
    assert rec.index == 7
    assert rec.sensor_presence_mask == 0b101
    assert rec.zone_enable_mask == 0x7F
    assert rec.zone_presence_mask == 0x03
    assert rec.timestamp == 1700000000


def test_decode_presence_history_detail_synthetic_round_trip():
    # 37-byte record: index(u16) sensor_mask(1) 7xtrigger(u16) zone_enable(1) zone_presence(1)
    # 7xtrigger(u16) timestamp(u32)
    sub_triggers = list(range(10, 17))
    zone_triggers = list(range(20, 27))
    raw = (
        struct.pack(">H", 3)
        + bytes([0b011])
        + struct.pack(">7H", *sub_triggers)
        + bytes([0x7F, 0x02])
        + struct.pack(">7H", *zone_triggers)
        + struct.pack(">I", 1700000100)
    )
    records = decode_presence_history(raw, detail=True)
    assert len(records) == 1
    rec = records[0]
    assert rec.index == 3
    assert rec.sub_sensor_triggers == tuple(sub_triggers)
    assert rec.zone_triggers == tuple(zone_triggers)
    assert rec.timestamp == 1700000100


def test_decode_light_history_synthetic():
    raw = struct.pack(">H", 1) + struct.pack(">I", 1700000200) + struct.pack(">H", 150)
    raw += struct.pack(">H", 2) + struct.pack(">I", 1700000260) + struct.pack(">H", 200)
    samples = decode_light_history(raw)
    assert len(samples) == 2
    assert samples[0].index == 1 and samples[0].timestamp == 1700000200 and samples[0].light_lux == 150
    assert samples[1].light_lux == 200


def test_sample_interval_round_trip():
    encoded = encode_sample_interval(2, 60)
    assert encoded == bytes([2]) + struct.pack(">H", 60)
    decoded = decode_sample_intervals(encoded)
    assert decoded == {2: 60}


def test_decode_sample_intervals_multi_record():
    raw = encode_sample_interval(0, 30) + encode_sample_interval(1, 45)
    assert decode_sample_intervals(raw) == {0: 30, 1: 45}


def test_decode_supported_tags_is_structural_only():
    assert decode_supported_tags(bytes.fromhex("0001010401010303")) == (0, 1, 1, 4, 1, 1, 3, 3)


def test_decode_dnd_and_pir_state():
    assert decode_dnd(b"\x01") is True
    assert decode_dnd(b"\x00") is False
    assert decode_pir_state(b"\x01") is True
    assert decode_pir_state(b"\x00") is False
    assert decode_pir_state(None) is None
