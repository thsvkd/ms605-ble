#!/usr/bin/env python3
"""
scan.py -- BLE advertisement scanner for Meross MS605 reconnaissance.

Passive/active BLE scanning only LISTENS to advertisement broadcasts; it does
NOT open a GATT connection to anything. It is therefore safe to run at any
time and does not require the target device's physical button to be pressed.

Usage:
    venv/bin/python scripts/scan.py --help
    venv/bin/python scripts/scan.py --timeout 15
    venv/bin/python scripts/scan.py --timeout 20 --json captures/scan.json
    venv/bin/python scripts/scan.py --self-test
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field

import ble_common as bc


@dataclass
class ScanEntry:
    address: str
    name: str | None
    rssi: int | None
    manufacturer_data: dict[str, str]  # "0x00E0" -> hex payload
    service_uuids: list[str]
    service_data: dict[str, str]  # uuid -> hex payload
    meross_candidate_reasons: list[str] = field(default_factory=list)

    @property
    def is_candidate(self) -> bool:
        return bool(self.meross_candidate_reasons)


def build_entry(address: str, name: str | None, rssi: int | None,
                 manufacturer_data: dict[int, bytes], service_uuids: list[str],
                 service_data: dict[str, bytes]) -> ScanEntry:
    mfg_hex = {f"0x{cid:04x}": bc.to_hex(payload) for cid, payload in manufacturer_data.items()}
    svc_data_hex = {uuid: bc.to_hex(bytes(payload)) for uuid, payload in service_data.items()}
    reasons = bc.is_meross_candidate(name, service_uuids, manufacturer_data)
    return ScanEntry(
        address=address,
        name=name,
        rssi=rssi,
        manufacturer_data=mfg_hex,
        service_uuids=list(service_uuids),
        service_data=svc_data_hex,
        meross_candidate_reasons=reasons,
    )


def format_table(entries: list[ScanEntry]) -> str:
    lines: list[str] = []
    ordered = sorted(entries, key=lambda e: (e.rssi is None, -(e.rssi or -999)))
    for e in ordered:
        marker = "*MEROSS?*" if e.is_candidate else "         "
        rssi_str = f"{e.rssi:5d} dBm" if e.rssi is not None else "   ? dBm"
        name = e.name or "(no name)"
        lines.append(f"{marker} {rssi_str}  {e.address}  {name}")
        for cid_hex, payload_hex in e.manufacturer_data.items():
            cid = int(cid_hex, 16)
            lines.append(f"              mfg {cid_hex} ({bc.describe_company_id(cid)}): {payload_hex}")
        if e.service_uuids:
            lines.append(f"              services: {', '.join(e.service_uuids)}")
        for uuid, payload_hex in e.service_data.items():
            lines.append(f"              service_data {uuid}: {payload_hex}")
        for reason in e.meross_candidate_reasons:
            lines.append(f"              [candidate] {reason}")
    if not ordered:
        lines.append("(no devices seen -- is the adapter powered on and unblocked?)")
    return "\n".join(lines)


async def run_scan(timeout: float, adapter: str | None) -> list[ScanEntry]:
    # Imported lazily so --help and --self-test work even if bleak/BlueZ is
    # unavailable in some environment (e.g. CI without a real adapter).
    from bleak import BleakScanner

    kwargs = {}
    if adapter:
        kwargs["bluez"] = {"adapter": adapter}

    print(f"Scanning for {timeout:.1f}s on adapter={adapter or '(default)'} ... "
          f"({bc.adapter_status_summary()})", file=sys.stderr)

    results = await BleakScanner.discover(timeout=timeout, return_adv=True, **kwargs)

    entries: list[ScanEntry] = []
    for address, (device, adv) in results.items():
        name = adv.local_name or device.name
        entries.append(build_entry(
            address=address,
            name=name,
            rssi=adv.rssi,
            manufacturer_data=dict(adv.manufacturer_data),
            service_uuids=list(adv.service_uuids or []),
            service_data=dict(adv.service_data or {}),
        ))
    return entries


def self_test() -> int:
    """Exercise build_entry()/format_table() against synthetic advertisement
    data -- no adapter, no BLE I/O, no network."""
    synthetic = [
        # Synthetic MS605-like advertisement: mfr-ID 0xFFFF with 0xC0
        # candidate device-type byte + MRBL_ name prefix + configured service.
        build_entry(
            address="AA:BB:CC:DD:EE:01",
            name="MRBL_ab12",
            rssi=-42,
            manufacturer_data={bc.MS605_ADV_COMPANY_ID: bytes([bc.MS605_SUBDEV_TYPE, 0x01, 0x02])},
            service_uuids=[bc.MS605_SERVICE_UUID, bc.MATTER_CHIPOBLE_SERVICE_UUID],
            service_data={},
        ),
        build_entry(
            address="AA:BB:CC:DD:EE:02",
            name="Unrelated Headphones",
            rssi=-70,
            manufacturer_data={0x004C: b"\x10"},
            service_uuids=[],
            service_data={},
        ),
        # A legacy Meross Wi-Fi device -- flagged, but explicitly as NOT the MS605.
        build_entry(
            address="AA:BB:CC:DD:EE:03",
            name=None,
            rssi=None,
            manufacturer_data={},
            service_uuids=[bc.MEROSS_LEGACY_SERVICE_UUID],
            service_data={},
        ),
    ]
    # The MS605 advert must trip all three strong signals (service, mfr sig, name).
    assert synthetic[0].is_candidate, "MS605 advert should be flagged as candidate"
    ms605_reasons = " ".join(synthetic[0].meross_candidate_reasons)
    assert "99E7BE30-0001" in ms605_reasons, ms605_reasons
    assert "STRONG" in ms605_reasons, ms605_reasons
    assert f"0x{bc.MS605_SUBDEV_TYPE:02x}" in ms605_reasons, ms605_reasons
    assert sum("STRONG" in r for r in synthetic[0].meross_candidate_reasons) >= 3, \
        synthetic[0].meross_candidate_reasons
    assert not synthetic[1].is_candidate, "unrelated headphones must not be flagged"
    assert synthetic[2].is_candidate, "legacy service UUID alone must still flag (as non-MS605)"
    assert any("NOT the MS605" in r for r in synthetic[2].meross_candidate_reasons), \
        synthetic[2].meross_candidate_reasons

    table = format_table(synthetic)
    assert "MEROSS?" in table
    assert "Unrelated Headphones" in table
    # sort order: -42 dBm should appear before -70 dBm, and the RSSI-less
    # entry should sort last.
    idx_42 = table.index("AA:BB:CC:DD:EE:01")
    idx_70 = table.index("AA:BB:CC:DD:EE:02")
    idx_none = table.index("AA:BB:CC:DD:EE:03")
    assert idx_42 < idx_70 < idx_none, "RSSI sort order (desc, None last) violated"

    # JSON round trip
    payload = [asdict(e) for e in synthetic]
    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)
    assert reloaded[0]["address"] == "AA:BB:CC:DD:EE:01"

    print("ALL scan.py SELF-TESTS PASSED")
    print(table)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan for BLE advertisements and highlight likely-Meross devices. "
                    "Passive listening only -- never connects to anything.",
    )
    parser.add_argument("--timeout", "-t", type=float, default=15.0,
                         help="scan duration in seconds (default: 15)")
    parser.add_argument("--adapter", "-a", default=None,
                         help="BlueZ adapter name, e.g. hci0 (default: system default adapter)")
    parser.add_argument("--json", metavar="PATH", default=None,
                         help="write full machine-readable results to this JSON file")
    parser.add_argument("--self-test", action="store_true",
                         help="run built-in self-tests against synthetic advertisement data "
                              "and exit (no adapter or BLE I/O used)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    try:
        entries = asyncio.run(run_scan(args.timeout, args.adapter))
    except Exception as exc:  # noqa: BLE001 -- top-level CLI error boundary
        print("ERROR: BLE scan failed.", file=sys.stderr)
        print(bc.friendly_ble_error(exc), file=sys.stderr)
        return 1

    print(format_table(entries))
    candidates = [e for e in entries if e.is_candidate]
    print(f"\n{len(entries)} device(s) seen, {len(candidates)} flagged as possible Meross candidates.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(e) for e in entries], f, indent=2)
        print(f"Wrote {len(entries)} entries to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
