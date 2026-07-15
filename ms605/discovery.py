"""ms605.discovery -- advertisement matching and BLE error diagnostics.

No bleak import here: `is_ms605_advertisement` and `friendly_ble_error` take
plain values (strings/bytes/exceptions), so this module stays importable
(and testable) even in environments without bleak installed.
"""

from __future__ import annotations

import os

from .protocol import is_ms605_advertisement  # re-exported for a single import surface

__all__ = ["is_ms605_advertisement", "adapter_status_summary", "friendly_ble_error"]


def adapter_status_summary() -> str:
    """Best-effort, dependency-free summary of local BLE adapters. Never
    raises (falls back to a note if /sys is unavailable)."""
    bt_class_dir = "/sys/class/bluetooth"
    try:
        adapters = sorted(os.listdir(bt_class_dir))
    except OSError:
        return "(could not enumerate /sys/class/bluetooth -- not on Linux/BlueZ?)"
    if not adapters:
        return "no Bluetooth adapters found under /sys/class/bluetooth"
    return f"adapters present: {', '.join(adapters)}"


def friendly_ble_error(exc: BaseException, address: str | None = None) -> str:
    """Turn a raw bleak/dbus exception into an actionable message."""
    text = str(exc)
    lowered = text.lower()
    hints: list[str] = []

    if "org.bluez" in lowered and "notready" in lowered.replace(" ", ""):
        hints.append("The Bluetooth adapter is not powered on (try: bluetoothctl power on).")
    if "org.bluez.error.failed" in lowered and "software caused connection abort" in lowered:
        hints.append(
            "The peer likely closed/refused the connection. The MS605 only accepts "
            "GATT connections for a short window after its physical button is "
            "pressed -- ask the device owner to press it again immediately before retrying."
        )
    if "device or resource busy" in lowered or "resource busy" in lowered:
        hints.append("Another process (BlueZ cache, another script) may be holding the connection.")
    if "no such device" in lowered or "does not exist" in lowered:
        hints.append(
            f"No BLE device found matching {address!r}." if address else "No BLE device found."
        )
        hints.append("Confirm the address with the 'scan' command and that the target is currently advertising.")
    if "timed out" in lowered or "timeout" in lowered:
        hints.append(
            "Connection attempt timed out. The MS605 only accepts connections briefly "
            "after a physical button press -- confirm the owner has just triggered "
            "pairing/config mode."
        )
    if "permission denied" in lowered:
        hints.append(
            "Permission denied talking to BlueZ. Check your user is in the 'bluetooth' "
            "group or that the adapter/D-Bus policy allows this process access."
        )
    if not hints:
        hints.append(f"No specific hint matched; see the raw error above. ({adapter_status_summary()})")

    return f"{text}\n  -> " + "\n  -> ".join(hints)
