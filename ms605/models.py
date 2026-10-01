"""ms605.models -- decoded value types for the MS605 TLV attributes.

Every decode/encode function here is pure (no I/O), mirroring the split in
ms605.protocol: that module handles the frame *envelope*, this one handles
the *meaning* of individual TLV attribute payloads. See docs/SPEC.md for the
public wire contract and confidence labels.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .errors import FrameError, ProfileError
from .protocol import (
    TAG_DETECT_MODE,
    TAG_NAMES,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_SEGMENT_MAP,
    TAG_SENSITIVITY,
    TAG_SUBSENSOR_ENABLE,
    TAG_ZONE_DISTANCES,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    ParsedFrame,
)

# ---------------------------------------------------------------------------
# Zone thresholds / config (existing surface, moved from ms605_ble.py)
# ---------------------------------------------------------------------------


@dataclass
class ZoneThreshold:
    trigger: int
    maintain: int


def decode_zone_thresholds(value: bytes) -> tuple[ZoneThreshold, ...]:
    if len(value) != 28:
        raise FrameError(f"tag {TAG_ZONE_THRESHOLDS} (zone thresholds) expected 28 bytes, got {len(value)}")
    zones = []
    for i in range(7):
        trig, maint = struct.unpack_from(">HH", value, i * 4)
        zones.append(ZoneThreshold(trig, maint))
    return tuple(zones)


def encode_zone_thresholds(pairs: Sequence[tuple[int, int]]) -> bytes:
    if len(pairs) != 7:
        raise ValueError(f"expected exactly 7 (trigger, maintain) pairs, got {len(pairs)}")
    out = bytearray()
    for trig, maint in pairs:
        if not (0 <= trig <= 0xFFFF and 0 <= maint <= 0xFFFF):
            raise ValueError(f"threshold out of u16 range: trigger={trig} maintain={maint}")
        out += struct.pack(">HH", trig, maint)
    return bytes(out)


def decode_zone_distances(value: bytes) -> tuple[float, ...]:
    """8 single-byte 0.1m-unit boundaries (leading 0 + 7 zone boundaries)."""
    return tuple(b / 10.0 for b in value)


@dataclass
class SubSensorTiming:
    """One sub-sensor's presence/absence duration setting, in seconds."""

    index: int
    presence_seconds: int
    absence_seconds: int


def decode_presence_absence_times(value: bytes, *, sub_sensor_count: int = 3) -> tuple[SubSensorTiming, ...]:
    """Decode one four-byte `[presence:u16 BE][absence:u16 BE]` stride per
    sub-sensor. The 16-byte payload reserves a fourth slot; this model exposes
    the three MS605 sub-sensors and ignores the reserved slot."""
    min_len = sub_sensor_count * 4
    if len(value) < min_len:
        raise FrameError(
            f"tag {TAG_PRESENCE_ABSENCE_TIMES} (presence/absence times) expected >= {min_len} bytes "
            f"for {sub_sensor_count} sub-sensors, got {len(value)}"
        )
    out = []
    for i in range(sub_sensor_count):
        presence, absence = struct.unpack_from(">HH", value, i * 4)
        out.append(SubSensorTiming(index=i, presence_seconds=presence, absence_seconds=absence))
    return tuple(out)


def encode_presence_absence_times(timings: Sequence[tuple[int, int]]) -> bytes:
    """Inverse of decode_presence_absence_times(): exactly 3 (presence,
    absence) second pairs, one per sub-sensor. Pads the reserved fourth slot
    with zeros to produce the 16-byte wire value."""
    if len(timings) != 3:
        raise ValueError(f"expected exactly 3 (presence, absence) pairs, got {len(timings)}")
    out = bytearray()
    for presence, absence in timings:
        if not (0 <= presence <= 0xFFFF and 0 <= absence <= 0xFFFF):
            raise ValueError(f"duration out of u16 range: presence={presence} absence={absence}")
        out += struct.pack(">HH", presence, absence)
    out += b"\x00" * 4  # reserved fourth slot
    return bytes(out)


@dataclass
class SubSensorZoneMap:
    """Which zones (0..6) a sub-sensor's segment-map bitmask assigns to it."""

    index: int
    zones: tuple[int, ...]
    mask: int


def decode_segment_map(
    value: bytes, *, sub_sensor_count: int = 3, zone_count: int = 7
) -> tuple[SubSensorZoneMap, ...]:
    """Decode one zone-assignment bitmask byte per sub-sensor. The wire value
    reserves a fourth byte; the MS605 exposes three sub-sensors."""
    if len(value) < sub_sensor_count:
        raise FrameError(
            f"tag {TAG_SEGMENT_MAP} (segment map) expected >= {sub_sensor_count} bytes, got {len(value)}"
        )
    out = []
    for i in range(sub_sensor_count):
        mask = value[i]
        zones = tuple(z for z in range(zone_count) if mask & (1 << z))
        out.append(SubSensorZoneMap(index=i, zones=zones, mask=mask))
    return tuple(out)


def encode_segment_map(zone_lists: Sequence[Sequence[int]], *, zone_count: int = 7) -> bytes:
    """Inverse of decode_segment_map(): exactly 3 lists of zone indices
    (0..6), one per sub-sensor. Pads the reserved fourth byte with zero."""
    if len(zone_lists) != 3:
        raise ValueError(f"expected exactly 3 sub-sensor zone lists, got {len(zone_lists)}")
    out = bytearray(4)
    for i, zones in enumerate(zone_lists):
        mask = 0
        for z in zones:
            if not 0 <= z < zone_count:
                raise ValueError(f"zone index out of range 0..{zone_count - 1}: {z}")
            mask |= 1 << z
        out[i] = mask
    return bytes(out)


def encode_subsensor_enable(enabled: Sequence[bool]) -> int:
    """Pack a per-sub-sensor enable bitmask (bit i = sub-sensor i)."""
    if not 1 <= len(enabled) <= 8:
        raise ValueError(f"expected 1-8 sub-sensor enable flags, got {len(enabled)}")
    return sum(1 << i for i, e in enumerate(enabled) if e)


@dataclass
class MS605Config:
    """Decoded result of MS605.read_config()."""

    sub_sensor_enable: int
    segment_map: tuple[SubSensorZoneMap, ...]
    presence_absence_times: tuple[SubSensorTiming, ...]
    zone_enable: int
    zone_thresholds: tuple[ZoneThreshold, ...]
    detect_mode: int
    zone_distances_m: tuple[float, ...]
    sensitivity: int
    raw_attributes: list

    def zones_enabled(self) -> tuple[bool, ...]:
        return tuple(bool(self.zone_enable & (1 << i)) for i in range(7))


def decode_config(response: ParsedFrame) -> MS605Config:
    """Decode a multi-read response frame (tags in READ_CONFIG_TAGS) into a
    MS605Config. Raises FrameError if an expected tag is missing or empty."""

    def require(tag: int) -> bytes:
        value = response.get(tag)
        if value is None:
            raise FrameError(f"config response missing expected tag {tag} ({TAG_NAMES.get(tag, '?')})")
        return value

    def require_byte(tag: int) -> int:
        value = require(tag)
        if not value:
            raise FrameError(f"config response tag {tag} ({TAG_NAMES.get(tag, '?')}) has an empty value")
        return value[0]

    return MS605Config(
        sub_sensor_enable=require_byte(TAG_SUBSENSOR_ENABLE),
        segment_map=decode_segment_map(require(TAG_SEGMENT_MAP)),
        presence_absence_times=decode_presence_absence_times(require(TAG_PRESENCE_ABSENCE_TIMES)),
        zone_enable=require_byte(TAG_ZONE_ENABLE),
        zone_thresholds=decode_zone_thresholds(require(TAG_ZONE_THRESHOLDS)),
        detect_mode=require_byte(TAG_DETECT_MODE),
        zone_distances_m=decode_zone_distances(require(TAG_ZONE_DISTANCES)),
        sensitivity=require_byte(TAG_SENSITIVITY),
        raw_attributes=response.attributes,
    )


# Fallback distances (metres) for the 7 radar zones if the device does not
# report them; matches the MS605 spec (0.8 m .. 6.0 m).
FALLBACK_DISTANCES_M: tuple[float, ...] = (0.8, 1.6, 2.4, 3.2, 4.0, 5.0, 6.0)


def zone_distances(cfg: MS605Config) -> tuple[float, ...]:
    """Return exactly 7 zone far-edge distances (metres)."""
    d = tuple(cfg.zone_distances_m or ())
    # tag53 carries 8 boundary markers (0.0 .. 6.0); the 7 zones are the
    # non-zero far edges. Be tolerant of 7- or 8-length encodings.
    if len(d) >= 8:
        d = d[1:8]
    if len(d) != 7:
        d = FALLBACK_DISTANCES_M
    return d


# ---------------------------------------------------------------------------
# Config profile -- the *writable* subset of MS605Config, captured so one
# sensor's settings can be cloned onto others (`ms605 clone`). Pure value type
# + JSON (de)serialisation; the device writes themselves live in
# MS605.apply_profile(). Kept here (not in the driver) so profiles can be built
# and round-tripped through files with no BLE dependency.
# ---------------------------------------------------------------------------

# The app models exactly 3 sub-sensors (APP_SUB_SENSOR_COUNT) even though the
# firmware reserves 4 slots -- mirror that here for tags 41/48/49.
PROFILE_SUB_SENSOR_COUNT = 3
PROFILE_FORMAT_ID = "ms605-config-profile"

# Ordered registry of cloneable sections: (key, human label). This is the
# single source of truth shared by ConfigProfile, the clone selection UI, and
# MS605.apply_profile(). zone_distances (tag53) is intentionally absent -- it is
# a device-reported physical mapping with no write path.
PROFILE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("sensitivity", "민감도 (tag61)"),
    ("detect_mode", "감지 모드 (tag52)"),
    ("zone_enable", "존 활성화 (tag50)"),
    ("zone_thresholds", "존 임계값 trigger/maintain (tag51)"),
    ("subsensor_zones", "센서별 구역 지정 Sensor1/2/3 (tag48)"),
    ("subsensor_timing", "센서별 presence/absence 지속시간 (tag49)"),
    ("subsensor_enable", "서브센서 enable 비트마스크 (tag41)"),
)
PROFILE_SECTION_KEYS: tuple[str, ...] = tuple(k for k, _ in PROFILE_SECTIONS)
PROFILE_SECTION_LABELS: dict[str, str] = dict(PROFILE_SECTIONS)
PROFILE_ZONE_COUNT = 7


def _is_int(x) -> bool:
    # bool is an int subclass; True/False are never a valid numeric setting.
    return isinstance(x, int) and not isinstance(x, bool)


def _check_seq(section: str, value, length: int) -> None:
    # str/bytes are sequences too, but never a valid list of settings.
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ProfileError(f"profile section {section!r}: expected a list of {length} entries, got {value!r}")


def _check_flags(section: str, value, length: int) -> None:
    _check_seq(section, value, length)
    if not all(isinstance(x, bool) for x in value):
        raise ProfileError(f"profile section {section!r}: expected {length} true/false flags, got {value!r}")


def _check_u16_pairs(section: str, value, length: int) -> None:
    _check_seq(section, value, length)
    for pair in value:
        if not (
            isinstance(pair, (list, tuple))
            and len(pair) == 2
            and all(_is_int(n) and 0 <= n <= 0xFFFF for n in pair)
        ):
            raise ProfileError(f"profile section {section!r}: expected (a, b) integer pairs in 0..65535, got {pair!r}")


@dataclass
class ConfigProfile:
    """The writable subset of an MS605's configuration, for cloning onto other
    sensors. Every section is optional: None means "not captured / do not
    write". See PROFILE_SECTIONS for the ordered registry and
    MS605.apply_profile() for the actual device writes."""

    sensitivity: int | None = None
    detect_mode: int | None = None
    zone_enable: list[bool] | None = None  # 7 zone on/off flags (tag50)
    zone_thresholds: list[tuple[int, int]] | None = None  # 7 (trigger, maintain) (tag51)
    subsensor_zones: list[list[int]] | None = None  # 3 lists of zone indices (tag48)
    subsensor_timing: list[tuple[int, int]] | None = None  # 3 (presence, absence) secs (tag49)
    subsensor_enable: list[bool] | None = None  # 3 sub-sensor on/off flags (tag41)
    source_name: str | None = None
    source_address: str | None = None

    @classmethod
    def from_config(
        cls,
        cfg: MS605Config,
        *,
        source_name: str | None = None,
        source_address: str | None = None,
    ) -> ConfigProfile:
        """Capture every writable section from a freshly-read MS605Config."""
        return cls(
            sensitivity=cfg.sensitivity,
            detect_mode=cfg.detect_mode,
            zone_enable=list(cfg.zones_enabled()),
            zone_thresholds=[(z.trigger, z.maintain) for z in cfg.zone_thresholds],
            subsensor_zones=[list(zm.zones) for zm in cfg.segment_map],
            subsensor_timing=[
                (t.presence_seconds, t.absence_seconds) for t in cfg.presence_absence_times
            ],
            subsensor_enable=[
                bool(cfg.sub_sensor_enable & (1 << i)) for i in range(PROFILE_SUB_SENSOR_COUNT)
            ],
            source_name=source_name,
            source_address=source_address,
        )

    def validate(self, sections: Iterable[str] | None = None) -> None:
        """Check every populated section's type, length and range up front, so
        a bad profile is rejected before any device write. `sections` limits
        the check to those keys (the ones about to be written; default: all).
        Raises ProfileError on the first problem; values are never coerced
        (e.g. "2" is rejected). MS605.apply_profile() calls this before its
        first write."""
        keys = set(PROFILE_SECTION_KEYS if sections is None else sections)
        for key, lo, hi in (("sensitivity", 1, 4), ("detect_mode", 1, 4)):
            value = getattr(self, key) if key in keys else None
            if value is not None and not (_is_int(value) and lo <= value <= hi):
                raise ProfileError(f"profile section {key!r}: expected an integer in {lo}..{hi}, got {value!r}")
        if "zone_enable" in keys and self.zone_enable is not None:
            _check_flags("zone_enable", self.zone_enable, PROFILE_ZONE_COUNT)
        if "zone_thresholds" in keys and self.zone_thresholds is not None:
            _check_u16_pairs("zone_thresholds", self.zone_thresholds, PROFILE_ZONE_COUNT)
        if "subsensor_zones" in keys and self.subsensor_zones is not None:
            _check_seq("subsensor_zones", self.subsensor_zones, PROFILE_SUB_SENSOR_COUNT)
            for zones in self.subsensor_zones:
                if not (
                    isinstance(zones, (list, tuple))
                    and all(_is_int(z) and 0 <= z < PROFILE_ZONE_COUNT for z in zones)
                ):
                    raise ProfileError(
                        f"profile section 'subsensor_zones': expected zone indices in 0..{PROFILE_ZONE_COUNT - 1}, "
                        f"got {zones!r}"
                    )
        if "subsensor_timing" in keys and self.subsensor_timing is not None:
            _check_u16_pairs("subsensor_timing", self.subsensor_timing, PROFILE_SUB_SENSOR_COUNT)
        if "subsensor_enable" in keys and self.subsensor_enable is not None:
            _check_flags("subsensor_enable", self.subsensor_enable, PROFILE_SUB_SENSOR_COUNT)

    def sections_present(self) -> tuple[str, ...]:
        """Section keys that actually carry a value (are cloneable), in registry order."""
        return tuple(k for k in PROFILE_SECTION_KEYS if getattr(self, k) is not None)

    def diff_sections(self, other: ConfigProfile, sections: Sequence[str]) -> list[str]:
        """Of `sections`, the keys whose value differs from `other`'s -- used to
        confirm an apply actually landed (re-read config vs. intended profile)."""

        def value(profile: ConfigProfile, key: str):
            v = getattr(profile, key)
            if key == "subsensor_zones" and v is not None:
                # tag48 is one bitmask per sub-sensor: order and repeats are not stored
                return [sorted(set(zones)) for zones in v]
            return v

        return [k for k in sections if value(self, k) != value(other, k)]

    def to_dict(self) -> dict:
        """JSON-serialisable envelope: metadata + only the populated sections
        (tuples flattened to lists so json.dump round-trips cleanly)."""
        sections: dict = {}
        for key in PROFILE_SECTION_KEYS:
            value = getattr(self, key)
            if value is None:
                continue
            if key in ("zone_thresholds", "subsensor_timing"):
                sections[key] = [list(pair) for pair in value]
            else:
                sections[key] = value
        return {
            "format": PROFILE_FORMAT_ID,
            "version": 1,
            "source_name": self.source_name,
            "source_address": self.source_address,
            "sections": sections,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConfigProfile:
        """Inverse of to_dict(); raises FrameError on a foreign/garbled file."""
        if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT_ID:
            raise FrameError(f"not an ms605 config profile (expected format={PROFILE_FORMAT_ID!r})")
        sections = data.get("sections") or {}
        if not isinstance(sections, dict):
            raise FrameError("profile 'sections' must be an object")
        unknown = set(sections) - set(PROFILE_SECTION_KEYS)
        if unknown:
            raise FrameError(f"unknown profile section(s): {sorted(unknown)}")

        # Values are taken as-is (no int()/bool() coercion) so validate() sees
        # exactly what the file said; only the pair sections are re-typed as
        # tuples afterwards, to match from_config() and round-trip equality.
        profile = cls(
            sensitivity=sections.get("sensitivity"),
            detect_mode=sections.get("detect_mode"),
            zone_enable=sections.get("zone_enable"),
            zone_thresholds=sections.get("zone_thresholds"),
            subsensor_zones=sections.get("subsensor_zones"),
            subsensor_timing=sections.get("subsensor_timing"),
            subsensor_enable=sections.get("subsensor_enable"),
            source_name=data.get("source_name"),
            source_address=data.get("source_address"),
        )
        profile.validate()
        for key in ("zone_thresholds", "subsensor_timing"):
            pairs = getattr(profile, key)
            if pairs is not None:
                setattr(profile, key, [(a, b) for a, b in pairs])
        return profile


# ---------------------------------------------------------------------------
# Sub-sensor status (tag 64)
# ---------------------------------------------------------------------------


@dataclass
class SubSensorStatus:
    """One sub-sensor's presence/absence bookkeeping, decoded from a tag-64
    payload. `has_presence` comes from the leading bitmask (byte 0); the two
    timestamps are epoch seconds stored in separate four-slot arrays."""

    index: int
    has_presence: bool
    presence_timestamp: int
    absence_timestamp: int


def decode_sub_sensor_status(value: bytes, *, sub_sensor_count: int = 3) -> tuple[SubSensorStatus, ...]:
    # Layout: mask(1) + two four-slot timestamp buffers. Slot i's
    # presence dword sits at [i*4+1 : i*4+5] and its absence dword at [i*4+17 : i*4+21]
    # so a three-sub-sensor read still needs bytes through the third absence field.
    min_len = (sub_sensor_count - 1) * 4 + 21
    if len(value) < min_len:
        raise FrameError(
            f"tag 64 (sub-sensor status) expected >= {min_len} bytes for "
            f"{sub_sensor_count} sub-sensors, got {len(value)}"
        )
    presence_mask = value[0]
    out = []
    for i in range(sub_sensor_count):
        stride = i * 4
        presence_ts = struct.unpack_from(">I", value, stride + 1)[0]
        absence_ts = struct.unpack_from(">I", value, stride + 17)[0]
        out.append(
            SubSensorStatus(
                index=i,
                has_presence=bool(presence_mask & (1 << i)),
                presence_timestamp=presence_ts,
                absence_timestamp=absence_ts,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Live radar output (tag 55)
# ---------------------------------------------------------------------------


@dataclass
class RadarZoneLive:
    """One zone's live radar snapshot, decoded from a tag-55 (live radar
    output) push: the zone's current signal energy alongside the
    *device-computed* trigger/maintain threshold it's being compared against
    right now. During auto-calibration (SPACE_LEARNING) the device adjusts
    these thresholds itself and streams the updated values here -- nothing
    on the host side computes or writes them."""

    index: int
    current_trigger: int
    current_maintain: int
    enabled: bool
    trigger_active: bool
    # Signed so transient negative calibration thresholds remain meaningful.
    trigger_threshold: int
    maintain_threshold: int


@dataclass
class RadarOutputSnapshot:
    sub_sensor_presence: tuple[bool, ...]
    zones: tuple[RadarZoneLive, ...]


def decode_radar_output(value: bytes, *, zone_count: int = 7) -> RadarOutputSnapshot:
    # Layout: one presence-mask byte + zone_count ten-byte records
    # (i16 current_trigger, i16
    # current_maintain, u8 enabled, u8 trigger_active, i16 trigger_threshold,
    # i16 maintain_threshold). Signed decoding preserves transient negative
    # calibration thresholds and is pinned by a synthetic regression test.
    min_len = 1 + zone_count * 10
    if len(value) < min_len:
        raise FrameError(
            f"tag 55 (live radar output) expected >= {min_len} bytes for "
            f"{zone_count} zones, got {len(value)}"
        )
    presence_mask = value[0]
    sub_sensor_presence = tuple(bool(presence_mask & (1 << i)) for i in range(3))
    zones = []
    for i in range(zone_count):
        off = 1 + i * 10
        cur_trig, cur_maint = struct.unpack_from(">hh", value, off)
        trig_thresh, maint_thresh = struct.unpack_from(">hh", value, off + 6)
        zones.append(
            RadarZoneLive(
                index=i,
                current_trigger=cur_trig,
                current_maintain=cur_maint,
                enabled=bool(value[off + 4]),
                trigger_active=bool(value[off + 5]),
                trigger_threshold=trig_thresh,
                maintain_threshold=maint_thresh,
            )
        )
    return RadarOutputSnapshot(sub_sensor_presence=sub_sensor_presence, zones=tuple(zones))


# ---------------------------------------------------------------------------
# Presence history (tags 58/59; experimental)
# ---------------------------------------------------------------------------


@dataclass
class PresenceHistoryRecord:
    """One presence-history entry. `detail` records (37 bytes) carry
    per-sub-sensor and per-zone trigger snapshots; `simple` records
    (9 bytes) carry only the coarse bitmasks. This layout remains experimental
    and is not part of the supported hardware contract."""

    index: int
    sensor_presence_mask: int
    zone_enable_mask: int
    zone_presence_mask: int
    timestamp: int
    sub_sensor_triggers: tuple[int, ...] = ()
    zone_triggers: tuple[int, ...] = ()


def decode_presence_history_simple(value: bytes) -> PresenceHistoryRecord:
    if len(value) != 9:
        raise FrameError(f"simple presence history record expected 9 bytes, got {len(value)}")
    index = struct.unpack_from(">H", value, 0)[0]
    sensor_mask = value[2]
    zone_enable_mask = value[3]
    zone_presence_mask = value[4]
    timestamp = struct.unpack_from(">I", value, 5)[0]
    return PresenceHistoryRecord(
        index=index,
        sensor_presence_mask=sensor_mask,
        zone_enable_mask=zone_enable_mask,
        zone_presence_mask=zone_presence_mask,
        timestamp=timestamp,
    )


def decode_presence_history_detail(value: bytes) -> PresenceHistoryRecord:
    if len(value) != 37:
        raise FrameError(f"detail presence history record expected 37 bytes, got {len(value)}")
    index = struct.unpack_from(">H", value, 0)[0]
    sensor_mask = value[2]
    sub_triggers = struct.unpack_from(">7H", value, 3)
    zone_enable_mask = value[17]
    zone_presence_mask = value[18]
    zone_triggers = struct.unpack_from(">7H", value, 19)
    timestamp = struct.unpack_from(">I", value, 33)[0]
    return PresenceHistoryRecord(
        index=index,
        sensor_presence_mask=sensor_mask,
        zone_enable_mask=zone_enable_mask,
        zone_presence_mask=zone_presence_mask,
        timestamp=timestamp,
        sub_sensor_triggers=sub_triggers,
        zone_triggers=zone_triggers,
    )


def decode_presence_history(raw: bytes, *, detail: bool = False) -> list[PresenceHistoryRecord]:
    record_size = 37 if detail else 9
    parser = decode_presence_history_detail if detail else decode_presence_history_simple
    usable = len(raw) - (len(raw) % record_size)
    return [parser(raw[i : i + record_size]) for i in range(0, usable, record_size)]


# ---------------------------------------------------------------------------
# Light history (tags 57/60; experimental)
# ---------------------------------------------------------------------------


@dataclass
class LightSample:
    index: int
    timestamp: int
    light_lux: int


def decode_light_history(raw: bytes) -> list[LightSample]:
    record_size = 8
    usable = len(raw) - (len(raw) % record_size)
    out = []
    for i in range(0, usable, record_size):
        rec = raw[i : i + record_size]
        index = struct.unpack_from(">H", rec, 0)[0]
        timestamp = struct.unpack_from(">I", rec, 2)[0]
        light = struct.unpack_from(">H", rec, 6)[0]
        out.append(LightSample(index=index, timestamp=timestamp, light_lux=light))
    return out


# ---------------------------------------------------------------------------
# Per-sub-sensor sample interval (tag 98; experimental)
# ---------------------------------------------------------------------------


def decode_sample_intervals(value: bytes) -> dict[int, int]:
    """0 or more (index:1, seconds:u16 BE) 3-byte records."""
    if len(value) % 3 != 0:
        raise FrameError(f"tag 98 (sample interval) value length {len(value)} not a multiple of 3")
    out: dict[int, int] = {}
    for i in range(0, len(value), 3):
        idx = value[i]
        secs = struct.unpack_from(">H", value, i + 1)[0]
        out[idx] = secs
    return out


def encode_sample_interval(sensor_index: int, seconds: int) -> bytes:
    if not 0 <= sensor_index <= 0xFF:
        raise ValueError(f"sensor_index out of byte range: {sensor_index}")
    if not 0 <= seconds <= 0xFFFF:
        raise ValueError(f"seconds out of u16 range: {seconds}")
    return bytes([sensor_index]) + struct.pack(">H", seconds)


# ---------------------------------------------------------------------------
# Tag 21 (version / supported-tags blob) -- structural decode only
# ---------------------------------------------------------------------------


def decode_supported_tags(value: bytes) -> tuple[int, ...]:
    """Return one integer per byte without choosing between the competing
    firmware-version and supported-tags interpretations."""
    return tuple(value)


def decode_dnd(value: bytes) -> bool:
    return bool(value) and value[0] != 0


def decode_pir_state(value: bytes | None) -> bool | None:
    if value is None:
        return None
    return bool(value) and value[0] != 0
