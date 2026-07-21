"""Offline checks for ms605.cli.ble (`ms605-driver`): argparse wiring only.
No BLE hardware needed."""

from __future__ import annotations

from ms605.cli.ble import _parse_timing_pair, _parse_zone_list, _parse_zone_pair, build_arg_parser


def test_scan_and_read_subcommands_parse():
    parser = build_arg_parser()
    assert parser.parse_args(["scan"]).command == "scan"
    assert parser.parse_args(["--address", "AA:BB", "read"]).command == "read"


def test_zone_pair_parsing_rejects_bad_input():
    assert _parse_zone_pair("95,40") == (95, 40)
    import argparse

    import pytest

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_zone_pair("95")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_zone_pair("a,b")


def test_new_subcommands_parse():
    parser = build_arg_parser()
    assert parser.parse_args(["--address", "AA:BB", "set-dnd", "1"]).state == 1
    assert parser.parse_args(["--address", "AA:BB", "read-dnd"]).command == "read-dnd"
    assert parser.parse_args(["--address", "AA:BB", "read-pir"]).command == "read-pir"
    assert parser.parse_args(["--address", "AA:BB", "read-subsensor-status"]).command == "read-subsensor-status"
    assert parser.parse_args(["--address", "AA:BB", "sync-time"]).command == "sync-time"
    hist = parser.parse_args(["--address", "AA:BB", "read-history", "light"])
    assert hist.kind == "light" and hist.detail is False
    zone_enable = parser.parse_args(
        ["--address", "AA:BB", "set-zone-enable", "1", "1", "1", "1", "1", "1", "0"]
    )
    assert zone_enable.flags == [1, 1, 1, 1, 1, 1, 0]


def test_set_subsensor_zones_and_timing_parse():
    parser = build_arg_parser()
    zones = parser.parse_args(
        ["--address", "AA:BB", "set-subsensor-zones", "0,1,2", "", "5,6"]
    )
    assert zones.zones == [[0, 1, 2], [], [5, 6]]
    timing = parser.parse_args(
        ["--address", "AA:BB", "set-subsensor-timing", "0,30", "5,60", "10,120"]
    )
    assert timing.timings == [(0, 30), (5, 60), (10, 120)]


def test_timing_pair_parsing_rejects_bad_input():
    import argparse

    import pytest

    assert _parse_timing_pair("0,30") == (0, 30)
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_timing_pair("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_timing_pair("a,b")


def test_zone_list_parsing():
    import argparse

    import pytest

    assert _parse_zone_list("") == []
    assert _parse_zone_list("0,1,2") == [0, 1, 2]
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_zone_list("7")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_zone_list("a")
