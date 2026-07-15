#!/usr/bin/env python3
"""
notify_logger.py -- subscribe to every notify/indicate characteristic on a
BLE device and log timestamped hex notifications, optionally sending one
write first to provoke a response.

WARNING: this script CONNECTS to the target. Only run it against the MS605
during a supervised session, immediately after the device owner has pressed
the device's physical pairing/config button.

Usage:
    venv/bin/python scripts/notify_logger.py --help
    venv/bin/python scripts/notify_logger.py AA:BB:CC:DD:EE:FF
    venv/bin/python scripts/notify_logger.py AA:BB:CC:DD:EE:FF --duration 30
    # subscribe to the MS605 notify char and provoke a response with a write:
    venv/bin/python scripts/notify_logger.py AA:BB:CC:DD:EE:FF \\
        --write 99e7be30-0002-4c6b-98a2-70fcb3471a72 55aac00006110161...
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import ble_common as bc


def format_line(char_uuid: str, data: bytes) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    ms = int((time.time() % 1) * 1000)
    return (f"[{ts}.{ms:03d}] NOTIFY {char_uuid} "
             f"len={len(data)} hex={bc.to_hex(data)} ascii={bc.to_ascii(data)!r}")


class Logger:
    """Tiny stateful sink: writes to stdout and an open log file handle, and
    counts notifications. Kept as a plain class (not a script-level global)
    so self_test() can exercise it without any file/BLE I/O."""

    def __init__(self, file_handle=None):
        self.file_handle = file_handle
        self.count = 0
        self.lines: list[str] = []

    def emit(self, char_uuid: str, data: bytes) -> None:
        line = format_line(char_uuid, data)
        self.count += 1
        self.lines.append(line)
        print(line)
        if self.file_handle is not None:
            self.file_handle.write(line + "\n")
            self.file_handle.flush()


async def run_logger(address: str, adapter: str | None, connect_timeout: float,
                      duration: float, out_path: Path,
                      writes: list[tuple[str, bytes]], write_delay: float,
                      char_filter: list[str] | None) -> int:
    from bleak import BleakClient

    kwargs = {}
    if adapter:
        kwargs["bluez"] = {"adapter": adapter}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        logger = Logger(file_handle=f)

        async with BleakClient(address, timeout=connect_timeout, **kwargs) as client:
            notify_chars = []
            for service in client.services:
                for char in service.characteristics:
                    if bc.NOTIFY_LIKE_PROPS & set(char.properties):
                        if char_filter and char.uuid.lower() not in {c.lower() for c in char_filter}:
                            continue
                        notify_chars.append(char)

            if not notify_chars:
                print("WARNING: no notify/indicate characteristics found "
                      "(or none matched --char filter). Nothing to subscribe to.",
                      file=sys.stderr)

            def make_callback(uuid: str):
                def _cb(_char, data: bytearray) -> None:
                    logger.emit(uuid, bytes(data))
                return _cb

            for char in notify_chars:
                await client.start_notify(char, make_callback(char.uuid))
                print(f"Subscribed to {char.uuid} (handle=0x{char.handle:04x})", file=sys.stderr)

            for char_uuid, payload in writes:
                await asyncio.sleep(write_delay)
                print(f"Writing {bc.to_hex(payload)} to {char_uuid} ...", file=sys.stderr)
                await client.write_gatt_char(char_uuid, payload)

            if duration > 0:
                print(f"Logging for {duration:.1f}s ... (Ctrl+C to stop early)", file=sys.stderr)
                try:
                    await asyncio.sleep(duration)
                except asyncio.CancelledError:
                    pass
            else:
                print("Logging until Ctrl+C ...", file=sys.stderr)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass

            for char in notify_chars:
                try:
                    await client.stop_notify(char)
                except Exception:
                    pass

        return logger.count


def self_test() -> int:
    """Exercise Logger/format_line() with synthetic notification bytes -- no
    adapter, no connection, no real file (uses an in-memory-like temp file)."""
    import io

    buf = io.StringIO()
    logger = Logger(file_handle=buf)
    logger.emit("0000b003-0000-1000-8000-00805f9b34fb", b"\x55\xaa\x00\x04")
    logger.emit("0000b003-0000-1000-8000-00805f9b34fb", b"\xaa\x55")

    assert logger.count == 2
    assert len(logger.lines) == 2
    assert "55aa0004" in logger.lines[0]
    assert "NOTIFY 0000b003" in logger.lines[0]
    written = buf.getvalue()
    assert written.count("NOTIFY") == 2
    assert written == "\n".join(logger.lines) + "\n"

    line = format_line("uuid-x", b"\x41\x42\x00")
    assert "ascii='AB.'" in line
    assert "len=3" in line
    assert "hex=414200" in line

    print("ALL notify_logger.py SELF-TESTS PASSED")
    print(written, end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Subscribe to all notify/indicate characteristics on a BLE device and "
                    "log timestamped hex notifications. WARNING: this CONNECTS to the target.",
    )
    parser.add_argument("address", nargs="?", default=None,
                         help="target BLE MAC address, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--adapter", "-a", default=None,
                         help="BlueZ adapter name, e.g. hci0 (default: system default adapter)")
    parser.add_argument("--connect-timeout", type=float, default=15.0,
                         help="seconds to wait for the GATT connection (default: 15)")
    parser.add_argument("--duration", type=float, default=0.0,
                         help="seconds to log for; 0 = run until Ctrl+C (default: 0)")
    parser.add_argument("--out", default=None,
                         help="log file path (default: '<script_dir>/../captures/"
                              "notify_<addr>_<ts>.log')")
    parser.add_argument("--write", nargs=2, action="append", default=[],
                         metavar=("CHAR_UUID", "HEX"),
                         help="after subscribing, write HEX to CHAR_UUID (repeatable). "
                              "HEX accepts '55aa..', '0x55aa..', or space/':'-separated bytes.")
    parser.add_argument("--write-delay", type=float, default=1.0,
                         help="seconds to wait after subscribing before sending each "
                              "--write (default: 1.0)")
    parser.add_argument("--char", action="append", default=None,
                         help="only subscribe to this characteristic UUID (repeatable); "
                              "default: subscribe to every notify/indicate characteristic")
    parser.add_argument("--self-test", action="store_true",
                         help="run built-in self-tests against synthetic notification bytes "
                              "and exit (no adapter or BLE I/O used)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.address:
        parser.error("the following arguments are required: address (unless --self-test)")

    try:
        writes = [(uuid, bc.parse_hex_arg(hex_str)) for uuid, hex_str in args.write]
    except bc.HexParseError as exc:
        parser.error(f"--write HEX parse error: {exc}")

    if args.out:
        out_path = Path(args.out)
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_addr = args.address.replace(":", "")
        out_path = Path(__file__).resolve().parent.parent / "captures" / f"notify_{safe_addr}_{ts}.log"

    try:
        count = asyncio.run(run_logger(
            address=args.address,
            adapter=args.adapter,
            connect_timeout=args.connect_timeout,
            duration=args.duration,
            out_path=out_path,
            writes=writes,
            write_delay=args.write_delay,
            char_filter=args.char,
        ))
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        print(f"Log written to {out_path}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level CLI error boundary
        print(f"ERROR: notify_logger failed for {args.address}.", file=sys.stderr)
        print(bc.friendly_ble_error(exc, address=args.address), file=sys.stderr)
        return 1

    print(f"\n{count} notification(s) logged to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
