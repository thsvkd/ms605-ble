"""Offline checks for ms605.cli.scheduled_calibrate (`ms605-schedule`):
scheduling math, summary formatting, argparse wiring. No BLE hardware needed."""

from __future__ import annotations

from datetime import datetime

import pytest

from ms605 import MS605
from ms605.cli.scheduled_calibrate import (
    ManagedDevice,
    build_parser,
    format_summary,
    resolve_target_datetime,
)


def test_evening_setup_rolls_over_to_next_day():
    now = datetime(2026, 7, 14, 20, 0, 0)
    assert resolve_target_datetime("02:00", now) == datetime(2026, 7, 15, 2, 0, 0)


def test_early_morning_not_yet_passed_stays_same_day():
    now = datetime(2026, 7, 14, 1, 0, 0)
    assert resolve_target_datetime("02:00", now) == datetime(2026, 7, 14, 2, 0, 0)


def test_exact_match_is_treated_as_already_passed():
    now = datetime(2026, 7, 14, 2, 0, 0)
    assert resolve_target_datetime("02:00", now) == datetime(2026, 7, 15, 2, 0, 0)


def test_invalid_hour_raises_value_error():
    with pytest.raises(ValueError):
        resolve_target_datetime("25:00", datetime(2026, 7, 14, 20, 0, 0))


def test_unparseable_format_raises_value_error():
    with pytest.raises(ValueError):
        resolve_target_datetime("abc", datetime(2026, 7, 14, 20, 0, 0))


def test_argparse_defaults():
    parser = build_parser()
    ns = parser.parse_args(["--at", "02:00"])
    assert ns.at == "02:00"
    assert ns.keepalive_interval == 15.0
    assert ns.calibration_timeout == 200.0
    assert ns.scan_secs == 6.0
    assert ns.connect_timeout == 12.0


def test_driver_exposes_ping_and_calibration():
    assert hasattr(MS605, "ping") and hasattr(MS605, "start_auto_calibration")


def test_format_summary_renders_status_labels_and_counts():
    ok = ManagedDevice(ms=None, device=None, name="Test-A", address="AA:AA", status="calibrated_ok")
    lost = ManagedDevice(ms=None, device=None, name="Test-B", address="BB:BB", status="lost", detail="boom")
    summary = format_summary([ok, lost])
    assert "보정 성공" in summary
    assert "연결 끊김" in summary
    assert "boom" in summary
    assert "1/2대 보정 성공" in summary


def test_format_summary_handles_empty_list():
    assert "연결된 센서가 없었습니다" in format_summary([])
