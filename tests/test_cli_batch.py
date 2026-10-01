"""Offline checks for the multi-sensor batch-calibrate path in ms605.cli.cli
(the `calibrate` subcommand that absorbed the former `ms605-schedule`):
scheduling math, summary formatting, argparse wiring, and an end-to-end run
on the simulator. No BLE hardware needed."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

import ms605.cli.cli as cli
import ms605.driver as driver_mod
from ms605 import MS605
from ms605.cli.cli import (
    SummaryRow,
    build_parser,
    format_batch_summary,
    resolve_target_datetime,
)
from ms605.protocol import TAG_DEVICE_ID
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet


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
    ok = SummaryRow(name="Test-A", address="AA:AA", status="calibrated_ok")
    lost = SummaryRow(name="Test-B", address="BB:BB", status="lost", detail="boom")
    summary = format_batch_summary([ok, lost])
    assert "보정 성공" in summary
    assert "연결 끊김" in summary
    assert "boom" in summary
    assert "1/2대 보정 성공" in summary


def test_format_batch_summary_handles_empty_list():
    assert "연결된 센서가 없었습니다" in format_batch_summary([])


# --------------------------------------------------------------------------
# smoke: the whole `calibrate --collect` command path on a SimFleet
# --------------------------------------------------------------------------
def test_calibrate_collect_end_to_end_on_a_sim_fleet(monkeypatch, tmp_path, capsys):
    speed = 100.0
    sim = SimFleet(3, speed=speed, connectable_window=None, calibration_secs=30)  # idle drop 0.3 s
    monkeypatch.setattr(driver_mod, "BleakScanner", sim)
    monkeypatch.setattr(driver_mod, "BleakClient", sim.client_factory)
    monkeypatch.setattr(MS605, "_scan_rssi", {})
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    prompts: list[str] = []

    async def operator(prompt=""):
        prompts.append(prompt)
        if len(prompts) == 1:  # collecting: Enter once every sensor is connected
            while not all(dev.connected for dev in sim.devices):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.5)  # longer than the idle drop: keep-alive holds the links
        return ""

    monkeypatch.setattr(cli, "ainput", operator)
    args = build_parser().parse_args(["calibrate", "--collect", "--keepalive-interval", str(15 / speed),
                                      "--calibration-timeout", str(200 / speed),
                                      "--log-file", str(tmp_path / "run.log"), "--scan-secs", "0.01"])

    async def run():
        code = await cli.run_batch_calibration(
            scan_secs=args.scan_secs,
            connect_timeout=args.connect_timeout,
            collect=args.collect,
            schedule=args.schedule,
            keepalive_interval=args.keepalive_interval,
            calibration_timeout=args.calibration_timeout,
            log_file=args.log_file,
            prefer_address=args.address,
        )
        return code, asyncio.all_tasks() == {asyncio.current_task()}

    code, no_stray_tasks = asyncio.run(run())
    out = capsys.readouterr().out

    assert (code, no_stray_tasks) == (0, True)
    assert len(prompts) == 2  # the collect Enter, then the fire gate
    assert "3/3대 보정 성공" in out and "연결 해제" in out
    assert "연결 끊김" not in out
    assert out.count("자동 보정 시작...") == 3 and out.count("보정 성공") >= 3
    assert "3/3대 보정 성공" in (tmp_path / "run.log").read_text(encoding="utf-8")
    for dev in sim.devices:
        assert dev.thresholds == list(DEFAULT_LEARNED_THRESHOLDS) and not dev.connected
    history = [json.loads(line) for line in (tmp_path / "cal_results" / "calibration_history.jsonl").open()]
    assert sorted(r["device_id"] for r in history) == sorted(d.tags[TAG_DEVICE_ID].hex() for d in sim.devices)
