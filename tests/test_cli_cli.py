"""Offline checks for ms605.cli.cli (the interactive app / `ms605` command):
rendering helpers and argparse wiring. No BLE hardware needed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ms605 import ConfigProfile
from ms605.cli.cli import (
    CALIBRATION_HISTORY_PATH,
    FALLBACK_DISTANCES_M,
    ManagedDevice,
    _format_sensor_presence,
    _format_zone_presence,
    build_calibration_record,
    build_parser,
    format_clone_summary,
    format_config_table,
    format_device_header,
    format_profile_table,
    load_profile,
    merge_thresholds,
    render_monitor,
    resolve_sections,
    save_calibration_record,
    save_profile,
    zone_distances,
)
from ms605.models import decode_config, decode_radar_output
from ms605.protocol import parse_frame

# Synthetic response and live-radar payload used to exercise rendering and
# merge logic without retaining observations from a physical sensor.
SYNTHETIC_CONFIG_FRAME = (
    "55aac0005a112a29000105300004030c70003100100005001e000a002d000f003c"
    "000000003200017f33001c005a0028005000230046001e003c0019003200140028"
    "000f001e000a34000102350008000810182028323c3d000104030001006573aa55"
)
SYNTHETIC_LIVE_RADAR = (
    "050078006e0101003c00280064005a0100003700230050004601010032001e003c"
    "00370100002d00190032002d0100002800140028002301010023000f001e001901"
    "00001e000a"
)


def test_synthetic_config_frame_decodes_to_seven_zones():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    assert len(cfg.zone_thresholds) == 7
    assert cfg.zone_thresholds[0].trigger == 90 and cfg.zone_thresholds[0].maintain == 40


def test_zone_distances_falls_back_when_absent():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    assert zone_distances(cfg) == FALLBACK_DISTANCES_M


def test_format_config_table_renders_distances_and_values():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    table = format_config_table(cfg)
    assert "0.8 m" in table and "90" in table


def test_merge_thresholds_edits_one_zone_keeps_others():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    new_t = [None, None, None, 135, None, None, None]
    new_m = [None] * 7
    pairs = merge_thresholds(cfg, new_t, new_m)
    assert len(pairs) == 7
    assert pairs[3] == (135, cfg.zone_thresholds[3].maintain)
    assert pairs[0] == (90, 40)


def test_decode_radar_output_from_synthetic_payload():
    snap = decode_radar_output(bytes.fromhex(SYNTHETIC_LIVE_RADAR))
    assert len(snap.zones) == 7
    assert snap.sub_sensor_presence == (True, False, True)
    z0 = snap.zones[0]
    assert (z0.current_trigger, z0.current_maintain) == (120, 110)
    assert (z0.trigger_threshold, z0.maintain_threshold) == (60, 40)
    assert z0.enabled and z0.trigger_active
    z6 = snap.zones[6]
    assert (z6.current_trigger, z6.current_maintain) == (30, 25)
    assert (z6.trigger_threshold, z6.maintain_threshold) == (30, 10)
    assert z6.enabled and not z6.trigger_active


def test_decode_radar_output_negative_threshold_reads_signed():
    # Regression: at the start of SPACE_LEARNING the firmware recomputes
    # thresholds signed (`current - margin`), which can underflow below zero.
    # The synthetic payload pins signed decoding for a negative threshold.
    zone0 = bytes.fromhex("0007" "0007" "01" "01" "ffdf" "ffcb")
    other = bytes.fromhex("000c" "000c" "01" "00" "0034" "0028")
    snap = decode_radar_output(bytes([0x01]) + zone0 + other * 6)
    z0 = snap.zones[0]
    assert (z0.current_trigger, z0.trigger_threshold, z0.maintain_threshold) == (7, -33, -53)
    assert z0.trigger_active
    assert snap.zones[1].trigger_threshold == 52


def test_format_device_header_renders_id_version_battery_light():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    info = {
        "device_id": bytes.fromhex("00112233445566778899aabbccddeeff000102"),
        "light": (150).to_bytes(2, "big"),
        "version": bytes.fromhex("0001010401010303"),
        "battery": bytes([100]),
    }
    header = format_device_header("MS605-test", "AA:BB:CC:DD:EE:FF", cfg, info)
    assert "Meross MS605" in header
    assert "150 lux" in header
    assert "100%" in header
    assert "0.8 m" in header


def test_argparse_defaults_and_subcommands():
    parser = build_parser()
    assert parser.parse_args([]).command is None
    assert parser.parse_args(["calibrate"]).command == "calibrate"
    assert parser.parse_args(["auto"]).command == "auto"

    za = parser.parse_args(
        ["set-zone", "95,40", "85,40", "75,40", "60,40", "55,40", "40,35", "35,28"]
    )
    assert len(za.pairs) == 7 and za.pairs[0] == (95, 40)

    assert parser.parse_args(["set-sensitivity", "3"]).level == 3
    assert parser.parse_args(["--address", "ABEA-123", "calibrate"]).address == "ABEA-123"
    assert parser.parse_args(["--device", "ms605", "read"]).address == "ms605"
    assert parser.parse_args(["read"]).address is None


def test_build_calibration_record_captures_committed_thresholds():
    cfg = decode_config(parse_frame(bytes.fromhex(SYNTHETIC_CONFIG_FRAME)))
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    record = build_calibration_record("MS605-test", "AA:BB:CC:DD:EE:FF", cfg, when=when)
    assert record["timestamp"] == when.isoformat()
    assert record["device_name"] == "MS605-test"
    assert record["device_address"] == "AA:BB:CC:DD:EE:FF"
    assert record["sensitivity"] == cfg.sensitivity
    assert len(record["zones"]) == 7
    assert record["zones"][0] == {
        "index": 0,
        "distance_m": FALLBACK_DISTANCES_M[0],
        "trigger": 90,
        "maintain": 40,
    }


def test_save_calibration_record_appends_jsonl(tmp_path):
    path = tmp_path / "nested" / "calibration_history.jsonl"
    record1 = {"a": 1}
    record2 = {"b": 2}
    save_calibration_record(record1, path=path)
    returned = save_calibration_record(record2, path=path)
    assert returned == path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [record1, record2]


def test_calibration_history_path_is_under_repo_cal_results():
    assert CALIBRATION_HISTORY_PATH.parent.name == "cal_results"
    assert CALIBRATION_HISTORY_PATH.name == "calibration_history.jsonl"


def test_argparse_new_subcommands():
    parser = build_parser()
    assert parser.parse_args(["read-dnd"]).command == "read-dnd"
    assert parser.parse_args(["set-dnd", "1"]).state == 1
    assert parser.parse_args(["read-pir"]).command == "read-pir"
    assert parser.parse_args(["read-subsensor-status"]).command == "read-subsensor-status"
    assert parser.parse_args(["sync-time"]).command == "sync-time"
    hist = parser.parse_args(["read-history", "presence", "--detail"])
    assert hist.kind == "presence" and hist.detail is True


def test_argparse_monitor_and_zone_subsensor_config_subcommands():
    parser = build_parser()
    assert parser.parse_args(["monitor"]).command == "monitor"
    assert parser.parse_args(["set-zone-enable"]).command == "set-zone-enable"
    assert parser.parse_args(["set-subsensor-zones"]).command == "set-subsensor-zones"
    assert parser.parse_args(["set-subsensor-timing"]).command == "set-subsensor-timing"


def test_format_sensor_presence_and_zone_presence():
    snap = decode_radar_output(bytes.fromhex(SYNTHETIC_LIVE_RADAR))
    assert _format_sensor_presence(snap) == "Sensor1=재실 Sensor2=부재 Sensor3=재실"
    zone_presence = _format_zone_presence(snap)
    assert len(zone_presence) == 7
    assert zone_presence[0] == "1"
    assert zone_presence[6] == "0"


# --------------------------------------------------------------------------
# clone subcommand: argparse wiring + profile file / section helpers
# --------------------------------------------------------------------------
def _sample_profile() -> ConfigProfile:
    return ConfigProfile(
        sensitivity=3,
        detect_mode=2,
        zone_enable=[True] * 7,
        zone_thresholds=[(95, 40)] * 7,
        subsensor_zones=[[0, 1], [2, 3], [4, 5, 6]],
        subsensor_timing=[(30, 30), (30, 30), (30, 25)],
        subsensor_enable=[True, True, False],
        source_name="sensor-A",
        source_address="AA:BB",
    )


def test_argparse_clone_defaults():
    ns = build_parser().parse_args(["clone"])
    assert ns.command == "clone"
    assert ns.source is None
    assert ns.save is None
    assert ns.from_file is None
    assert ns.only is None
    assert ns.skip is None
    assert ns.no_apply is False
    assert ns.yes is False


def test_argparse_clone_options():
    ns = build_parser().parse_args(
        ["clone", "--source", "ms605-A", "--save", "p.json", "--only", "zone_thresholds", "-y"]
    )
    assert ns.source == "ms605-A"
    assert ns.save == "p.json"
    assert ns.only == "zone_thresholds"
    assert ns.yes is True


def test_argparse_clone_from_file_and_skip():
    ns = build_parser().parse_args(
        ["clone", "--from-file", "p.json", "--skip", "detect_mode,subsensor_enable", "--no-apply"]
    )
    assert ns.from_file == "p.json"
    assert ns.skip == "detect_mode,subsensor_enable"
    assert ns.no_apply is True


def test_save_load_profile_round_trip(tmp_path):
    profile = _sample_profile()
    path = tmp_path / "sub" / "profile.json"
    saved = save_profile(profile, path)
    assert saved == path
    assert load_profile(path) == profile


def test_resolve_sections_default_is_all_present():
    profile = ConfigProfile(sensitivity=3, zone_thresholds=[(1, 2)] * 7)
    assert resolve_sections(profile) == ["sensitivity", "zone_thresholds"]


def test_resolve_sections_only_and_skip():
    profile = _sample_profile()
    assert resolve_sections(profile, only="zone_thresholds sensitivity") == [
        "sensitivity",
        "zone_thresholds",
    ]
    got = resolve_sections(profile, skip="detect_mode,subsensor_enable")
    assert "detect_mode" not in got and "subsensor_enable" not in got
    assert "sensitivity" in got


def test_resolve_sections_rejects_only_plus_skip():
    with pytest.raises(ValueError):
        resolve_sections(_sample_profile(), only="sensitivity", skip="detect_mode")


def test_resolve_sections_rejects_unknown_key():
    with pytest.raises(ValueError):
        resolve_sections(_sample_profile(), only="not_a_section")


def test_format_profile_table_lists_present_sections():
    out = format_profile_table(_sample_profile())
    assert "민감도" in out
    assert "HIGH" in out  # sensitivity 3 -> HIGH
    assert "sensor-A" in out  # source metadata line


def test_render_monitor_builds_a_frame_from_synthetic_payload():
    from ms605.cli import _ui

    snap = decode_radar_output(bytes.fromhex(SYNTHETIC_LIVE_RADAR))
    panel = render_monitor({"snap": snap, "pir": True, "ts": "12:00:00"}, FALLBACK_DISTANCES_M)
    # renders without raising, and carries the per-zone values + presence header
    with _ui.console.capture() as cap:
        _ui.console.print(panel)
    text = cap.get()
    assert "실시간 감지값 모니터링" in text
    assert "Z0" in text and "Z6" in text
    assert "120/60" in text


def test_render_monitor_waiting_state_has_no_zones():
    from ms605.cli import _ui

    with _ui.console.capture() as cap:
        _ui.console.print(render_monitor({"snap": None, "pir": None, "ts": "12:00:00"}, FALLBACK_DISTANCES_M))
    assert "데이터 수신 대기 중" in cap.get()


def test_format_clone_summary_counts_successes():
    devices = [
        ManagedDevice(ms=None, device=None, name="A", address="a", status="clone_ok"),
        ManagedDevice(ms=None, device=None, name="B", address="b", status="clone_error"),
    ]
    summary = format_clone_summary(devices, ["sensitivity"])
    assert "1/2대 적용 성공" in summary
    assert "적용 항목: sensitivity" in summary
