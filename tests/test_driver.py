"""Offline checks for the MS605 driver's API surface -- no BLE hardware
needed. Constructing an MS605 does not touch the radio (only connect()/
scan() do), so these checks run without an adapter."""

from __future__ import annotations

from ms605 import MS605

EXISTING_METHODS = [
    "scan",
    "connect",
    "disconnect",
    "reconnect",
    "ping",
    "read_config",
    "read_raw",
    "set_sensitivity",
    "set_zone_thresholds",
    "set_detect_mode",
    "set_live_output",
    "start_auto_calibration",
]

NEW_METHODS = [
    "set_time",
    "read_sub_sensor_status",
    "set_zone_enable",
    "set_subsensor_config",
    "set_subsensor_zones",
    "set_subsensor_timing",
    "read_pir_state",
    "set_dnd",
    "read_dnd",
    "set_sample_interval",
    "read_sample_interval",
    "read_presence_history",
    "read_light_history",
    "remove_push_handler",
]


def test_driver_exposes_existing_api():
    for name in EXISTING_METHODS:
        assert hasattr(MS605, name), f"MS605 is missing {name}()"


def test_driver_exposes_new_protocol_coverage():
    for name in NEW_METHODS:
        assert hasattr(MS605, name), f"MS605 is missing {name}() (see docs/APK_PROTOCOL.md)"


def test_construction_does_not_touch_the_radio():
    ms = MS605("AA:BB:CC:DD:EE:FF")
    assert ms.is_connected is False


def test_remove_push_handler_undoes_add_and_is_safe_when_absent():
    ms = MS605("AA:BB:CC:DD:EE:FF")

    def handler(_frame):
        pass

    ms.remove_push_handler(handler)  # never added -- must not raise
    ms.add_push_handler(handler)
    assert handler in ms._push_handlers
    ms.remove_push_handler(handler)
    assert handler not in ms._push_handlers
    ms.remove_push_handler(handler)  # already removed -- must not raise
