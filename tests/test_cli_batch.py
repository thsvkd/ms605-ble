"""Offline checks for the multi-sensor batch-calibrate path in ms605.cli.cli
(the `calibrate` subcommand that absorbed the former `ms605-schedule`):
scheduling math, summary formatting, argparse wiring. No BLE hardware needed."""

from __future__ import annotations

from datetime import datetime

import pytest

from ms605 import MS605
from ms605.cli.cli import (
    ManagedDevice,
    build_parser,
    format_batch_summary,
    resolve_target_datetime,
)


def test_evening_schedule_rolls_over_to_next_day():
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


def test_calibrate_argparse_defaults():
    ns = build_parser().parse_args(["calibrate"])
    assert ns.command == "calibrate"
    assert ns.collect is False
    assert ns.schedule is None
    assert ns.keepalive_interval == 15.0
    assert ns.calibration_timeout == 200.0
    assert ns.log_file is None


def test_calibrate_argparse_collect_and_schedule():
    ns = build_parser().parse_args(["calibrate", "--collect", "--schedule", "02:00"])
    assert ns.collect is True
    assert ns.schedule == "02:00"


def test_calibrate_aliases_share_the_batch_options():
    for alias in ("auto", "auto-calibrate"):
        ns = build_parser().parse_args([alias, "--collect"])
        assert ns.collect is True


def test_calibrate_keeps_global_address_before_subcommand():
    ns = build_parser().parse_args(["--address", "ABEA-123", "calibrate", "--collect"])
    assert ns.address == "ABEA-123"
    assert ns.collect is True


def test_driver_exposes_ping_and_calibration():
    assert hasattr(MS605, "ping") and hasattr(MS605, "start_auto_calibration")


def test_format_batch_summary_renders_status_labels_and_counts():
    ok = ManagedDevice(ms=None, device=None, name="Test-A", address="AA:AA", status="calibrated_ok")
    lost = ManagedDevice(
        ms=None, device=None, name="Test-B", address="BB:BB", status="lost", detail="boom"
    )
    summary = format_batch_summary([ok, lost])
    assert "보정 성공" in summary
    assert "연결 끊김" in summary
    assert "boom" in summary
    assert "1/2대 보정 성공" in summary


def test_format_batch_summary_handles_empty_list():
    assert "연결된 센서가 없었습니다" in format_batch_summary([])
