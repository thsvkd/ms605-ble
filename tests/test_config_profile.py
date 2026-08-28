"""Offline checks for the config-clone surface: ConfigProfile capture/
serialisation (ms605.models) and MS605.apply_profile()'s write dispatch
(ms605.driver). No BLE hardware needed -- apply_profile() is exercised against
a duck-typed fake that just records which setters it would call."""

from __future__ import annotations

import asyncio

import pytest

from ms605 import ConfigProfile
from ms605.driver import MS605
from ms605.errors import FrameError
from ms605.models import (
    PROFILE_SECTION_KEYS,
    decode_config,
)
from ms605.protocol import parse_frame

# Synthetic full config response shared conceptually with test_cli_cli.py.
SYNTHETIC_CONFIG_FRAME = (
    "55aac0005a112a29000105300004030c70003100100005001e000a002d000f003c"
    "000000003200017f33001c005a0028005000230046001e003c0019003200140028"
    "000f001e000a34000102350008000810182028323c3d000104030001006573aa55"
)


def _synthetic_config():
    return decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))


def _sample_profile() -> ConfigProfile:
    return ConfigProfile(
        sensitivity=3,
        detect_mode=2,
        zone_enable=[True, True, True, False, False, False, False],
        zone_thresholds=[(95, 40), (85, 40), (75, 40), (60, 40), (55, 40), (40, 35), (35, 28)],
        subsensor_zones=[[0, 1, 2], [3, 4], [5, 6]],
        subsensor_timing=[(30, 30), (30, 30), (30, 25)],
        subsensor_enable=[True, True, False],
        source_name="sensor-A",
        source_address="AA:BB:CC:DD:EE:FF",
    )


# --------------------------------------------------------------------------
# capture + serialisation
# --------------------------------------------------------------------------
def test_from_config_populates_every_section():
    profile = ConfigProfile.from_config(_synthetic_config(), source_name="A", source_address="addr")
    assert set(profile.sections_present()) == set(PROFILE_SECTION_KEYS)
    assert len(profile.zone_thresholds) == 7
    assert len(profile.zone_enable) == 7
    assert len(profile.subsensor_zones) == 3
    assert len(profile.subsensor_timing) == 3
    assert len(profile.subsensor_enable) == 3
    assert profile.source_name == "A"


def test_to_dict_from_dict_round_trip():
    profile = _sample_profile()
    restored = ConfigProfile.from_dict(profile.to_dict())
    assert restored == profile


def test_to_dict_omits_absent_sections():
    profile = ConfigProfile(sensitivity=2)
    data = profile.to_dict()
    assert data["sections"] == {"sensitivity": 2}
    assert data["format"] == "ms605-config-profile"


def test_from_dict_rejects_foreign_payload():
    with pytest.raises(FrameError):
        ConfigProfile.from_dict({"format": "something-else", "sections": {}})


def test_from_dict_rejects_unknown_section():
    with pytest.raises(FrameError):
        ConfigProfile.from_dict(
            {"format": "ms605-config-profile", "sections": {"bogus": 1}}
        )


def test_sections_present_is_registry_ordered():
    profile = ConfigProfile(subsensor_enable=[True], sensitivity=1)
    # declared out of order, but reported in PROFILE_SECTION_KEYS order
    assert profile.sections_present() == ("sensitivity", "subsensor_enable")


def test_diff_sections_flags_only_changed_keys():
    a = _sample_profile()
    b = ConfigProfile.from_dict(a.to_dict())
    b.sensitivity = 1  # diverge one section
    assert a.diff_sections(b, PROFILE_SECTION_KEYS) == ["sensitivity"]
    assert a.diff_sections(b, ["zone_thresholds"]) == []


# --------------------------------------------------------------------------
# apply_profile() write dispatch
# --------------------------------------------------------------------------
class _RecordingMS:
    """Duck-typed stand-in for MS605 that records setter calls instead of
    talking to a radio, so apply_profile()'s dispatch/order can be checked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def set_sensitivity(self, v, *, timeout=None):
        self.calls.append(("sensitivity", v))

    async def set_detect_mode(self, v, *, timeout=None):
        self.calls.append(("detect_mode", v))

    async def set_zone_enable(self, v, *, timeout=None):
        self.calls.append(("zone_enable", list(v)))

    async def set_zone_thresholds(self, v, *, timeout=None):
        self.calls.append(("zone_thresholds", list(v)))

    async def set_subsensor_zones(self, v, *, timeout=None):
        self.calls.append(("subsensor_zones", [list(z) for z in v]))

    async def set_subsensor_timing(self, v, *, timeout=None):
        self.calls.append(("subsensor_timing", list(v)))

    async def set_subsensor_config(self, *, enabled=None, timeout=None):
        self.calls.append(("subsensor_enable", list(enabled)))


def _apply(profile, sections=None):
    fake = _RecordingMS()
    applied = asyncio.run(MS605.apply_profile(fake, profile, sections))
    return fake, applied


def test_apply_profile_writes_all_sections():
    fake, applied = _apply(_sample_profile())
    written = [name for name, _ in fake.calls]
    assert set(written) == set(PROFILE_SECTION_KEYS)
    assert set(applied) == set(PROFILE_SECTION_KEYS)


def test_apply_profile_sensitivity_before_zone_thresholds():
    # a preset sensitivity level reloads the threshold table, so the cloned
    # per-zone values must be written *after* sensitivity to win.
    fake, _ = _apply(_sample_profile())
    order = [name for name, _ in fake.calls]
    assert order.index("sensitivity") < order.index("zone_thresholds")


def test_apply_profile_skips_space_learning_detect_mode():
    profile = ConfigProfile(detect_mode=4, sensitivity=2)  # 4 == SPACE_LEARNING
    fake, applied = _apply(profile)
    assert "detect_mode" not in applied
    assert ("detect_mode", 4) not in fake.calls
    assert "sensitivity" in applied


def test_apply_profile_section_filter():
    fake, applied = _apply(_sample_profile(), ["zone_thresholds"])
    assert applied == ["zone_thresholds"]
    assert [name for name, _ in fake.calls] == ["zone_thresholds"]


def test_apply_profile_skips_none_sections():
    fake, applied = _apply(ConfigProfile(sensitivity=1))
    assert applied == ["sensitivity"]


def test_apply_profile_unknown_section_raises():
    with pytest.raises(ValueError):
        _apply(_sample_profile(), ["not_a_section"])
