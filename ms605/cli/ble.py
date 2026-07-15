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
    print(f"segment_map         : {config.segment_map.hex()}")
    print(f"presence/absence times: {config.presence_absence_times}")


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

    sub.add_parser(
        "calibrate",
        help="Start auto-calibration (space-learning) and wait for the result push.",
        description="Write tag52=4 (SPACE_LEARNING), send periodic keep-alives to keep "
        "the link alive, and wait for the tag62 result push (success/failure).",
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
        elif args.command == "calibrate":
            print("starting auto-calibration; this can take up to ~3 minutes...")
            ok = await device.start_auto_calibration()
            print("calibration succeeded" if ok else "calibration reported failure")
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
