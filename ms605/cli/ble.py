"""ms605.cli.ble -- low-level protocol driver CLI.

Thin CLI over ms605.driver.MS605 for scripting single operations or
inspecting frames. Hardware commands need --address (get it from `scan`).
For an interactive, button-press-aware experience use `ms605` (ms605.cli.cli)
instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ms605 import MS605, DetectMode, MS605Config, MS605Error, Sensitivity
from ms605.discovery import adapter_status_summary
from ms605.protocol import WRITE_TIMEOUT_S


def _parse_zone_pair(text: str) -> tuple[int, int]:
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'trigger,maintain', got {text!r}")
    try:
        trig, maint = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"non-integer threshold in {text!r}") from exc
    if not (0 <= trig <= 0xFFFF and 0 <= maint <= 0xFFFF):
        raise argparse.ArgumentTypeError(f"threshold out of range (0-65535) in {text!r}")
    return trig, maint


def _parse_timing_pair(text: str) -> tuple[int, int]:
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'presence_secs,absence_secs', got {text!r}")
    try:
        presence, absence = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"non-integer duration in {text!r}") from exc
    if not (0 <= presence <= 0xFFFF and 0 <= absence <= 0xFFFF):
        raise argparse.ArgumentTypeError(f"duration out of range (0-65535) in {text!r}")
    return presence, absence


def _parse_zone_list(text: str) -> list[int]:
    text = text.strip()
    if not text:
        return []
    try:
        zones = [int(z) for z in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"non-integer zone index in {text!r}") from exc
    if any(not (0 <= z <= 6) for z in zones):
        raise argparse.ArgumentTypeError(f"zone index out of range 0-6 in {text!r}")
    return zones


def _print_config(config: MS605Config) -> None:
    def enum_name(enum_cls: type, value: int) -> str:
        try:
            return enum_cls(value).name  # type: ignore[call-arg]
        except ValueError:
            return "unknown"

    print(f"sensitivity         : {config.sensitivity} ({enum_name(Sensitivity, config.sensitivity)})")
    print(f"detect_mode         : {config.detect_mode} ({enum_name(DetectMode, config.detect_mode)})")
    print(f"zone_enable         : 0b{config.zone_enable:07b} -> {config.zones_enabled()}")
    print(f"zone_distances (m)  : {config.zone_distances_m}")
    print("zone_thresholds (trigger, maintain):")
    for i, z in enumerate(config.zone_thresholds, start=1):
        print(f"  zone {i}: trigger={z.trigger} maintain={z.maintain}")
    print(f"sub_sensor_enable   : 0b{config.sub_sensor_enable:03b}")
    print("sub-sensor zones (Sensor1/2/3 -> assigned zones):")
    for zm in config.segment_map:
        print(f"  Sensor{zm.index + 1}: zones={list(zm.zones)}  (mask=0b{zm.mask:07b})")
    print("sub-sensor timing (presence/absence seconds):")
    for t in config.presence_absence_times:
        print(f"  Sensor{t.index + 1}: presence={t.presence_seconds}s  absence={t.absence_seconds}s")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ms605-driver",
        description=(
            "Low-level control driver CLI for the Meross MS605 BLE presence sensor\n"
            "(com.meross.ble2 binary TLV protocol; see ms605.protocol's module\n"
            "docstring for the full reverse-engineered spec). This is the thin\n"
            "protocol layer; for an interactive, button-press-aware experience use\n"
            "the `ms605` command instead."
        ),
        epilog=(
            "commands\n"
            "  scan               list nearby MS605 devices (address + name)\n"
            "  read               connect to --address and print decoded config\n"
            "  set-sensitivity N  set radar sensitivity 1=LOW 2=MED 3=HIGH 4=CUSTOM\n"
            "  set-zone P0..P6    write all 7 zone 'trigger,maintain' pairs\n"
            "  set-subsensor-zones Z1 Z2 Z3   assign zones (0-6) to Sensor1/2/3\n"
            "  set-subsensor-timing T1 T2 T3  set Sensor1/2/3 presence/absence seconds\n"
            "  calibrate          start space-learning; keep the link alive and wait\n"
            "                     for the tag62 result push (up to ~3 min)\n"
            "\n"
            "hardware notes\n"
            "  * the MS605 only accepts a connection briefly after its physical button\n"
            "    is pressed, and BLE is single-link (close the Meross app first).\n"
            "  * all hardware commands (everything except 'scan') need --address, taken\n"
            "    from a 'scan' run (a CoreBluetooth UUID on macOS, a MAC on Linux).\n"
            "\n"
            "examples\n"
            "  uv run pytest                                     # offline protocol tests\n"
            "  ms605-driver scan\n"
            "  ms605-driver --address <ADDR> read\n"
            "  ms605-driver --address <ADDR> set-sensitivity 3\n"
            "  ms605-driver --address <ADDR> set-zone 95,40 85,40 75,40 60,40 55,40 40,35 35,28\n"
            "  ms605-driver --address <ADDR> calibrate"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--address",
        help="BLE address/identifier of the MS605 (required for all hardware "
        "commands; get it from a 'scan' run)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=WRITE_TIMEOUT_S,
        help="Per-command response timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=5.0,
        help="Scan duration in seconds for the 'scan' command (default: %(default)s)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser(
        "scan",
        help="Scan for nearby MS605 devices and print their address/name.",
        description="Scan for `--scan-timeout` seconds and print each MS605's address and name.",
    )
    sub.add_parser(
        "read",
        help="Connect to --address and print the decoded device configuration.",
        description="Connect to --address and print the decoded config (sensitivity, "
        "detect mode, zone enable, zone distances, and per-zone thresholds).",
    )

    p_sens = sub.add_parser(
        "set-sensitivity",
        help="Set radar sensitivity.",
        description="Write tag61 radar sensitivity: 1=LOW 2=MEDIUM 3=HIGH 4=CUSTOM.",
    )
    p_sens.add_argument("level", type=int, choices=[1, 2, 3, 4], help="1=LOW 2=MEDIUM 3=HIGH 4=CUSTOM")

    p_zone = sub.add_parser(
        "set-zone",
        help="Set all 7 zone (trigger,maintain) threshold pairs.",
        description="Write tag51: exactly 7 (trigger, maintain) pairs, one per zone "
        "(zone 0..6, near→far).",
    )
    p_zone.add_argument(
        "pairs",
        type=_parse_zone_pair,
        nargs=7,
        metavar="TRIGGER,MAINTAIN",
        help="Exactly 7 'trigger,maintain' pairs, one per zone (e.g. 95,40).",
    )

    p_zone_enable = sub.add_parser(
        "set-zone-enable",
        help="Enable/disable individual zones (tag50 bitmask).",
        description="Write tag50: one 0/1 flag per zone (zone 0..6, near→far).",
    )
    p_zone_enable.add_argument(
        "flags",
        type=int,
        choices=[0, 1],
        nargs=7,
        metavar="0|1",
        help="Exactly 7 flags, one per zone (e.g. 1 1 1 1 1 1 0).",
    )

    sub.add_parser(
        "calibrate",
        help="Start auto-calibration (space-learning) and wait for the result push.",
        description="Write tag52=4 (SPACE_LEARNING), send periodic keep-alives to keep "
        "the link alive, and wait for the tag62 result push (success/failure).",
    )

    p_sensor_zones = sub.add_parser(
        "set-subsensor-zones",
        help="Assign zones to Sensor1/2/3 (tag48 segment map).",
        description="Write tag48: which zones (0-6) feed each of the 3 sub-sensors. "
        "Give exactly 3 comma-separated zone-index lists (e.g. '0,1,2' or '' for none), "
        "one per sub-sensor, in Sensor1/2/3 order.",
    )
    p_sensor_zones.add_argument(
        "zones",
        type=_parse_zone_list,
        nargs=3,
        metavar="Z,Z,...",
        help="Zone indices (0-6) assigned to this sub-sensor, comma-separated (empty = none).",
    )

    p_sensor_timing = sub.add_parser(
        "set-subsensor-timing",
        help="Set Sensor1/2/3 presence/absence durations (tag49).",
        description="Write tag49: each sub-sensor's (presence, absence) duration in "
        "seconds. Give exactly 3 'presence_secs,absence_secs' pairs, one per sub-sensor, "
        "in Sensor1/2/3 order.",
    )
    p_sensor_timing.add_argument(
        "timings",
        type=_parse_timing_pair,
        nargs=3,
        metavar="PRESENCE,ABSENCE",
        help="Presence/absence duration in seconds for this sub-sensor (e.g. 0,30).",
    )

    p_dnd = sub.add_parser(
        "set-dnd",
        help="Set the do-not-disturb toggle (tag32).",
        description="Write tag32 (do-not-disturb): 1=on, 0=off.",
    )
    p_dnd.add_argument("state", type=int, choices=[0, 1], help="1=on 0=off")

    sub.add_parser(
        "read-dnd",
        help="Read the do-not-disturb toggle (tag32).",
    )
    sub.add_parser(
        "read-pir",
        help="Read the PIR state (tag56).",
    )
    sub.add_parser(
        "read-subsensor-status",
        help="Read per-sub-sensor presence status (tag64).",
    )
    sub.add_parser(
        "sync-time",
        help="Write the current UTC time to the device (tag33).",
    )
    p_history = sub.add_parser(
        "read-history",
        help="Read presence/light history (tags 57-60). Unverified against real hardware.",
        description="Read presence-history (--kind presence, tags 58/59) or "
        "light-history (--kind light, tags 57/60) records. Experimental; "
        "hardware behavior and pagination are not verified.",
    )
    p_history.add_argument("kind", choices=["presence", "light"], help="Which history to read.")
    p_history.add_argument(
        "--detail", action="store_true", help="For 'presence': request the 37-byte detail record format."
    )

    return parser


async def _run_command(args: argparse.Namespace) -> int:
    if args.command == "scan":
        devices = await MS605.scan(timeout=args.scan_timeout)
        if not devices:
            print("No MS605 devices found.")
            return 1
        for d in devices:
            print(f"{d.address}  {d.name or '(no name)'}")
        return 0

    if not args.address:
        print("error: --address is required for this command", file=sys.stderr)
        return 2

    device = MS605(args.address)
    try:
        await device.connect(timeout=args.timeout)
        if args.command == "read":
            config = await device.read_config(timeout=args.timeout)
            _print_config(config)
        elif args.command == "set-sensitivity":
            await device.set_sensitivity(args.level, timeout=args.timeout)
            print(f"sensitivity set to {args.level}")
        elif args.command == "set-zone":
            await device.set_zone_thresholds(args.pairs, timeout=args.timeout)
            print("zone thresholds updated")
        elif args.command == "set-zone-enable":
            flags = [bool(f) for f in args.flags]
            await device.set_zone_enable(flags, timeout=args.timeout)
            print(f"zone enable mask updated: {flags}")
        elif args.command == "calibrate":
            print("starting auto-calibration; this can take up to ~3 minutes...")
            ok = await device.start_auto_calibration()
            print("calibration succeeded" if ok else "calibration reported failure")
        elif args.command == "set-subsensor-zones":
            await device.set_subsensor_zones(args.zones, timeout=args.timeout)
            print(f"sub-sensor zone assignment updated: {args.zones}")
        elif args.command == "set-subsensor-timing":
            await device.set_subsensor_timing(args.timings, timeout=args.timeout)
            print(f"sub-sensor presence/absence timing updated: {args.timings}")
        elif args.command == "set-dnd":
            await device.set_dnd(bool(args.state), timeout=args.timeout)
            print(f"DND set to {bool(args.state)}")
        elif args.command == "read-dnd":
            print(f"DND: {await device.read_dnd(timeout=args.timeout)}")
        elif args.command == "read-pir":
            print(f"PIR state: {await device.read_pir_state(timeout=args.timeout)}")
        elif args.command == "read-subsensor-status":
            for status in await device.read_sub_sensor_status(timeout=args.timeout):
                print(
                    f"sub-sensor {status.index}: presence={status.has_presence} "
                    f"presence_ts={status.presence_timestamp} absence_ts={status.absence_timestamp}"
                )
        elif args.command == "sync-time":
            await device.set_time(timeout=args.timeout)
            print("device clock synced to host UTC time")
        elif args.command == "read-history":
            if args.kind == "presence":
                records = await device.read_presence_history(detail=args.detail, timeout=args.timeout)
            else:
                records = await device.read_light_history(timeout=args.timeout)
            if not records:
                print("(no history records)")
            for rec in records:
                print(rec)
        return 0
    except MS605Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await device.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    print(adapter_status_summary(), file=sys.stderr)
    return asyncio.run(_run_command(args))


if __name__ == "__main__":
    sys.exit(main())
