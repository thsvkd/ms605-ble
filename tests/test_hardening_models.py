"""Regression tests for the M0 base-review hardening of ms605.models / ms605.protocol
(profile validation, frame resync, multi-status helpers, empty config values)."""

import copy

import pytest

from ms605.errors import FrameError, MS605Error, ProfileError
from ms605.models import ConfigProfile, decode_config
from ms605.protocol import (
    MAX_FRAME_BODY_LEN,
    TAG_DETECT_MODE,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_SEGMENT_MAP,
    TAG_SENSITIVITY,
    TAG_STATUS,
    TAG_SUBSENSOR_ENABLE,
    TAG_ZONE_DISTANCES,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    FrameReassembler,
    build_command,
    build_frame_raw,
    parse_frame,
)


def _good_profile() -> ConfigProfile:
    return ConfigProfile(
        sensitivity=3,
        detect_mode=2,
        zone_enable=[True] * 7,
        zone_thresholds=[(95, 40)] * 7,
        subsensor_zones=[[0, 1], [2, 3], [4, 5, 6]],
        subsensor_timing=[(30, 30), (30, 30), (30, 25)],
        subsensor_enable=[True, True, False],
    )


def _envelope(**sections) -> dict:
    return {"format": "ms605-config-profile", "version": 1, "sections": sections}


# --------------------------------------------------------------------------
# 1. ConfigProfile.validate()
# --------------------------------------------------------------------------
def test_validate_accepts_good_and_empty_profiles():
    _good_profile().validate()
    ConfigProfile().validate()


def test_profile_error_is_ms605_and_value_error():
    assert issubclass(ProfileError, MS605Error) and issubclass(ProfileError, ValueError)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("sensitivity", 0),
        ("sensitivity", 5),
        ("sensitivity", "2"),
        ("sensitivity", True),
        ("sensitivity", 2.0),
        ("detect_mode", 9),
        ("detect_mode", "1"),
        ("zone_enable", [True, True, True]),  # would silently be written as mask 07
        ("zone_enable", [True] * 8),
        ("zone_enable", [1] * 7),
        ("zone_enable", ["false"] * 7),
        ("zone_enable", "ttttttt"),
        ("zone_thresholds", [(1, 2)] * 6),
        ("zone_thresholds", [(1, 2)] * 6 + [(1, 0x10000)]),
        ("zone_thresholds", [(1, 2)] * 6 + [(-1, 2)]),
        ("zone_thresholds", [(1, 2)] * 6 + [("2", 3)]),
        ("zone_thresholds", [(1, 2)] * 6 + [(1, 2, 3)]),
        ("subsensor_zones", [[0], [1]]),
        ("subsensor_zones", [[0], [1], [7]]),
        ("subsensor_zones", [[0], [1], [-1]]),
        ("subsensor_zones", [[0], [1], ["2"]]),
        ("subsensor_zones", [[0], [1], "2"]),
        ("subsensor_timing", [(1, 2)] * 2),
        ("subsensor_timing", [(1, 2), (1, 2), (1, 0x10000)]),
        ("subsensor_enable", [True]),
        ("subsensor_enable", [True, False, 1]),
    ],
)
def test_validate_rejects_bad_section(field, bad):
    profile = _good_profile()
    setattr(profile, field, bad)
    with pytest.raises(ProfileError, match=field):
        profile.validate()


def test_validate_can_be_limited_to_the_sections_being_written():
    profile = _good_profile()
    profile.sensitivity = 0  # e.g. a factory/unknown tag 61 on the source
    profile.validate(["zone_thresholds", "detect_mode"])
    with pytest.raises(ProfileError, match="sensitivity"):
        profile.validate(["sensitivity", "zone_thresholds"])
    with pytest.raises(ProfileError, match="sensitivity"):
        profile.validate()


@pytest.mark.parametrize(
    "section, bad",
    [
        ("zone_enable", [True, True, True]),
        ("zone_enable", [1, 1, 1, 1, 1, 1, 1]),
        ("sensitivity", "2"),
        ("zone_thresholds", [[1, "2"]] * 7),
        ("subsensor_timing", [[1, 2]] * 4),
    ],
)
def test_from_dict_validates(section, bad):
    with pytest.raises(ProfileError):
        ConfigProfile.from_dict(_envelope(**{section: bad}))


def test_from_dict_rejects_non_object_sections():
    with pytest.raises(FrameError):
        ConfigProfile.from_dict({"format": "ms605-config-profile", "sections": [1, 2]})


def test_from_dict_round_trip_still_equal_after_validation():
    profile = _good_profile()
    data = copy.deepcopy(profile.to_dict())
    assert ConfigProfile.from_dict(data) == profile


# --------------------------------------------------------------------------
# 2. FrameReassembler tail check + length cap
# --------------------------------------------------------------------------
def _frame(msg_id: int, level: int = 1) -> bytes:
    return build_command([(TAG_SENSITIVITY, bytes([level]))], msg_id=msg_id)


def test_reassembler_corrupt_tail_does_not_swallow_next_frame():
    good = _frame(11)
    corrupt = bytearray(_frame(10))
    corrupt[-1] ^= 0xFF  # tail AA 55 -> AA AA
    out = FrameReassembler().feed(bytes(corrupt) + good)
    assert out == [good]


def test_reassembler_corrupt_tail_resyncs_across_chunks():
    good = _frame(11)
    corrupt = bytearray(_frame(10))
    corrupt[-2] = 0x00
    reassembler = FrameReassembler()
    stream = bytes(corrupt) + good
    out = []
    for i in range(0, len(stream), 5):
        out.extend(reassembler.feed(stream[i : i + 5]))
    assert out == [good]


def test_reassembler_oversize_declared_length_is_garbage_not_a_stall():
    good = _frame(12)
    bogus_header = bytes.fromhex("55aac0ffff")
    reassembler = FrameReassembler()
    # Without the cap this header makes the buffer wait for ~64 KiB, so the
    # following good frame never comes out.
    assert reassembler.feed(bogus_header + good) == [good]
    assert reassembler.feed(_frame(13)) == [_frame(13)]


def test_reassembler_logs_a_frame_dropped_for_its_declared_size(caplog):
    good = _frame(12)
    oversize_header = b"\x55\xaa\xc0" + (MAX_FRAME_BODY_LEN + 1).to_bytes(2, "big")
    with caplog.at_level("WARNING", logger="ms605.protocol"):
        assert FrameReassembler().feed(oversize_header + good) == [good]
    assert any("MAX_FRAME_BODY_LEN" in r.getMessage() for r in caplog.records)


def test_reassembler_accepts_frame_at_length_cap():
    big = build_frame_raw([(TAG_SENSITIVITY, bytes(MAX_FRAME_BODY_LEN - 2 - 3))], msg_id=0)
    assert len(big) == 5 + MAX_FRAME_BODY_LEN + 4
    assert FrameReassembler().feed(big) == [big]


# --------------------------------------------------------------------------
# 3. ParsedFrame.statuses() / first_error_status()
# --------------------------------------------------------------------------
def _response(*status_bytes: int):
    attrs = [(TAG_STATUS, bytes([s])) for s in status_bytes]
    return parse_frame(build_frame_raw(attrs, msg_id=5, trigger_src=0x00))


def test_statuses_lists_every_tag3_value():
    assert _response(0, 0, 7).statuses() == [0, 0, 7]


def test_first_error_status_sees_later_failure_that_status_misses():
    resp = _response(0, 3, 9)
    assert resp.status() == 0  # the old first-only check reports success
    assert resp.first_error_status() == 3


def test_first_error_status_none_when_all_ok_or_absent():
    assert _response(0, 0).first_error_status() is None
    assert _response().first_error_status() is None
    assert _response().statuses() == []


# --------------------------------------------------------------------------
# 4. decode_config empty values
# --------------------------------------------------------------------------
def _config_attrs() -> dict[int, bytes]:
    return {
        TAG_SUBSENSOR_ENABLE: b"\x07",
        TAG_SEGMENT_MAP: bytes([0x03, 0x0C, 0x70, 0x00]),
        TAG_PRESENCE_ABSENCE_TIMES: bytes(16),
        TAG_ZONE_ENABLE: b"\x7f",
        TAG_ZONE_THRESHOLDS: bytes(28),
        TAG_DETECT_MODE: b"\x01",
        TAG_ZONE_DISTANCES: bytes(range(8)),
        TAG_SENSITIVITY: b"\x02",
    }


def _config_response(**overrides):
    attrs = _config_attrs()
    attrs.update({int(k.removeprefix("t")): v for k, v in overrides.items()})
    return parse_frame(build_frame_raw(list(attrs.items()), msg_id=3, trigger_src=0x00))


def test_decode_config_baseline_decodes():
    assert decode_config(_config_response()).sensitivity == 2


@pytest.mark.parametrize(
    "tag", [TAG_SUBSENSOR_ENABLE, TAG_ZONE_ENABLE, TAG_DETECT_MODE, TAG_SENSITIVITY]
)
def test_decode_config_empty_value_raises_frame_error(tag):
    with pytest.raises(FrameError):
        decode_config(_config_response(**{f"t{tag}": b""}))


def test_diff_sections_ignores_order_and_repeats_within_subsensor_zones():
    # tag48 is one bitmask per sub-sensor: the read-back is always sorted and unique
    intended = ConfigProfile(subsensor_zones=[[2, 0], [3, 3], [4]])
    read_back = ConfigProfile(subsensor_zones=[[0, 2], [3], [4]])
    assert intended.diff_sections(read_back, ["subsensor_zones"]) == []
    assert intended.diff_sections(ConfigProfile(subsensor_zones=[[0], [3], [4]]), ["subsensor_zones"]) == [
        "subsensor_zones"
    ]
