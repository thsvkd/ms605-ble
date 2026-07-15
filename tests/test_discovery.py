"""Offline checks for ms605.discovery -- no BLE hardware needed."""

from __future__ import annotations

from ms605.discovery import adapter_status_summary, friendly_ble_error, is_ms605_advertisement


def test_reexports_is_ms605_advertisement():
    assert is_ms605_advertisement("RFBL_1234", None, None) is True


def test_adapter_status_summary_never_raises():
    assert isinstance(adapter_status_summary(), str)


def test_friendly_ble_error_adds_button_hint_for_connection_abort():
    exc = Exception("org.bluez.Error.Failed: Software caused connection abort")
    msg = friendly_ble_error(exc, address="AA:BB:CC:DD:EE:FF")
    assert "button" in msg.lower()


def test_friendly_ble_error_falls_back_to_generic_hint():
    exc = Exception("some completely unrelated failure")
    msg = friendly_ble_error(exc)
    assert "No specific hint matched" in msg
