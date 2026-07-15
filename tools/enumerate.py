#!/usr/bin/env python3
"""
enumerate.py -- connect to a BLE device and dump its full GATT tree.

WARNING: this script CONNECTS to the target. Only run it against the MS605
during a supervised session, immediately after the device owner has pressed
the device's physical pairing/config button (the device only accepts GATT
connections for a short window afterwards).

Usage:
    venv/bin/python scripts/enumerate.py --help
    venv/bin/python scripts/enumerate.py AA:BB:CC:DD:EE:FF
    venv/bin/python scripts/enumerate.py AA:BB:CC:DD:EE:FF --no-read --outdir gatt/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ble_common as bc


@dataclass
class DescriptorInfo:
    uuid: str
    handle: int


@dataclass
class CharacteristicInfo:
    uuid: str
    handle: int
    properties: list[str]
    descriptors: list[DescriptorInfo] = field(default_factory=list)
    read_value_hex: str | None = None
    read_value_ascii: str | None = None
    read_error: str | None = None


@dataclass
class ServiceInfo:
    uuid: str
    handle: int
    characteristics: list[CharacteristicInfo] = field(default_factory=list)


@dataclass
class GattDump:
    address: str
    name: str | None
    mtu_size: int | None
    generated_at: str
    services: list[ServiceInfo] = field(default_factory=list)


def annotate_uuid(uuid: str) -> str:
    """Append a short known-role annotation to a UUID, if recognized."""
    u = uuid.lower()
    known = {
        bc.MS605_SERVICE_UUID: "*** MS605 BLE-config service (CONFIRMED, com.meross.ble2) ***",
        bc.MS605_WRITE_CHAR_UUID: "*** MS605 WRITE characteristic (send TLV commands here) ***",
        bc.MS605_NOTIFY_CHAR_UUID: "*** MS605 NOTIFY characteristic (subscribe for TLV responses/pushes) ***",
        bc.MEROSS_LEGACY_SERVICE_UUID: "legacy Meross Wi-Fi-device BLE-config service (NOT the MS605)",
        bc.MEROSS_LEGACY_WRITE_CHAR_UUID: "legacy Meross write characteristic (NOT the MS605)",
        bc.MEROSS_LEGACY_NOTIFY_CHAR_UUID: "legacy Meross notify characteristic (NOT the MS605)",
        bc.CCCD_UUID: "Client Characteristic Configuration Descriptor (standard)",
        bc.MATTER_CHIPOBLE_SERVICE_UUID: "Matter/CHIPoBLE commissioning service (standard; MS605 uses it for Matter join only)",
        bc.MATTER_C1_WRITE_UUID: "Matter/CHIPoBLE C1 write characteristic (standard)",
        bc.MATTER_C2_INDICATE_UUID: "Matter/CHIPoBLE C2 indicate characteristic (standard)",
        bc.MATTER_C3_READ_UUID: "Matter/CHIPoBLE C3 read characteristic (standard)",
    }
    if u in known:
        return f"{uuid}  [{known[u]}]"
    return uuid


async def dump_gatt(address: str, adapter: str | None, connect_timeout: float,
                     do_read: bool, read_timeout: float) -> GattDump:
    from bleak import BleakClient

    kwargs = {}
    if adapter:
        kwargs["bluez"] = {"adapter": adapter}

    async with BleakClient(address, timeout=connect_timeout, **kwargs) as client:
        mtu = None
        try:
            mtu = client.mtu_size
        except Exception:
            pass

        dump = GattDump(
            address=address,
            name=getattr(client, "name", None),
            mtu_size=mtu,
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )

        for service in client.services:
            svc_info = ServiceInfo(uuid=service.uuid, handle=service.handle)
            for char in service.characteristics:
                char_info = CharacteristicInfo(
                    uuid=char.uuid,
                    handle=char.handle,
                    properties=list(char.properties),
                )
                for desc in char.descriptors:
                    char_info.descriptors.append(DescriptorInfo(uuid=desc.uuid, handle=desc.handle))

                if do_read and "read" in char.properties:
                    try:
                        value = await asyncio.wait_for(
                            client.read_gatt_char(char), timeout=read_timeout
                        )
                        char_info.read_value_hex = bc.to_hex(bytes(value))
                        char_info.read_value_ascii = bc.to_ascii(bytes(value))
                    except Exception as exc:  # noqa: BLE001
                        char_info.read_error = str(exc)

                svc_info.characteristics.append(char_info)
            dump.services.append(svc_info)

        return dump


def render_report(dump: GattDump) -> str:
    lines = [
        f"GATT report for {dump.address}",
        f"  name: {dump.name!r}",
        f"  mtu_size: {dump.mtu_size}",
        f"  generated_at: {dump.generated_at}",
        "",
    ]
    for svc in dump.services:
        lines.append(f"Service {annotate_uuid(svc.uuid)}  (handle=0x{svc.handle:04x})")
        for char in svc.characteristics:
            props = ",".join(char.properties)
            lines.append(f"  Characteristic {annotate_uuid(char.uuid)}  "
                         f"(handle=0x{char.handle:04x}, props=[{props}])")
            if char.read_value_hex is not None:
                lines.append(f"    read: hex={char.read_value_hex}  ascii={char.read_value_ascii!r}")
            elif char.read_error is not None:
                lines.append(f"    read FAILED: {char.read_error}")
            for desc in char.descriptors:
                lines.append(f"    Descriptor {annotate_uuid(desc.uuid)}  (handle=0x{desc.handle:04x})")
        lines.append("")
    if not dump.services:
        lines.append("(no services discovered)")
    return "\n".join(lines)


def self_test() -> int:
    """Exercise dataclass construction + render_report()/annotate_uuid() with
    a synthetic GATT tree -- no adapter, no connection."""
    dump = GattDump(
        address="AA:BB:CC:DD:EE:FF",
        name="MS605-TEST",
        mtu_size=185,
        generated_at="2026-01-01T00:00:00",
        services=[
            ServiceInfo(
                uuid=bc.MS605_SERVICE_UUID,
                handle=0x0010,
                characteristics=[
                    CharacteristicInfo(
                        uuid=bc.MS605_WRITE_CHAR_UUID,
                        handle=0x0011,
                        properties=["write-without-response"],
                    ),
                    CharacteristicInfo(
                        uuid=bc.MS605_NOTIFY_CHAR_UUID,
                        handle=0x0012,
                        properties=["notify"],
                        descriptors=[DescriptorInfo(uuid=bc.CCCD_UUID, handle=0x0013)],
                    ),
                ],
            ),
            ServiceInfo(
                uuid="0000180a-0000-1000-8000-00805f9b34fb",
                handle=0x0020,
                characteristics=[
                    CharacteristicInfo(
                        uuid="00002a29-0000-1000-8000-00805f9b34fb",
                        handle=0x0021,
                        properties=["read"],
                        read_value_hex="4d65726f7373",
                        read_value_ascii="Meross",
                    ),
                    CharacteristicInfo(
                        uuid="00002a26-0000-1000-8000-00805f9b34fb",
                        handle=0x0022,
                        properties=["read"],
                        read_error="Access Denied (needs encryption)",
                    ),
                ],
            ),
        ],
    )

    report = render_report(dump)
    assert "MS605 BLE-config service (CONFIRMED" in report, report
    assert "MS605 WRITE characteristic" in report, report
    assert "hex=4d65726f7373" in report
    assert "ascii='Meross'" in report
    assert "read FAILED: Access Denied" in report
    assert "CCCD" in report or "Client Characteristic" in report

    # JSON round trip
    payload = asdict(dump)
    dumped = json.dumps(payload, indent=2)
    reloaded = json.loads(dumped)
    assert reloaded["address"] == "AA:BB:CC:DD:EE:FF"
    assert reloaded["services"][0]["characteristics"][0]["uuid"] == bc.MS605_WRITE_CHAR_UUID

    print("ALL enumerate.py SELF-TESTS PASSED")
    print(report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect to a BLE device and dump its full GATT tree "
                    "(services/characteristics/descriptors), reading every "
                    "readable characteristic. WARNING: this CONNECTS to the "
                    "target device.",
    )
    parser.add_argument("address", nargs="?", default=None,
                         help="target BLE MAC address, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--adapter", "-a", default=None,
                         help="BlueZ adapter name, e.g. hci0 (default: system default adapter)")
    parser.add_argument("--connect-timeout", type=float, default=15.0,
                         help="seconds to wait for the GATT connection (default: 15)")
    parser.add_argument("--read-timeout", type=float, default=10.0,
                         help="per-characteristic read timeout in seconds (default: 10)")
    parser.add_argument("--no-read", action="store_true",
                         help="skip reading characteristic values; just dump the tree structure")
    parser.add_argument("--outdir", default=None,
                         help="directory to write gatt_<addr>_<ts>.json/.txt into "
                              "(default: '<script_dir>/../gatt')")
    parser.add_argument("--self-test", action="store_true",
                         help="run built-in self-tests against a synthetic GATT tree and exit "
                              "(no adapter or BLE I/O used)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.address:
        parser.error("the following arguments are required: address (unless --self-test)")

    outdir = Path(args.outdir) if args.outdir else Path(__file__).resolve().parent.parent / "gatt"
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        dump = asyncio.run(dump_gatt(
            address=args.address,
            adapter=args.adapter,
            connect_timeout=args.connect_timeout,
            do_read=not args.no_read,
            read_timeout=args.read_timeout,
        ))
    except Exception as exc:  # noqa: BLE001 -- top-level CLI error boundary
        print(f"ERROR: could not enumerate GATT tree for {args.address}.", file=sys.stderr)
        print(bc.friendly_ble_error(exc, address=args.address), file=sys.stderr)
        return 1

    report = render_report(dump)
    print(report)

    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_addr = args.address.replace(":", "")
    json_path = outdir / f"gatt_{safe_addr}_{ts}.json"
    txt_path = outdir / f"gatt_{safe_addr}_{ts}.txt"
    with open(json_path, "w") as f:
        json.dump(asdict(dump), f, indent=2)
    with open(txt_path, "w") as f:
        f.write(report + "\n")

    print(f"\nWrote {json_path}")
    print(f"Wrote {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
