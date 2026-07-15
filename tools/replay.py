#!/usr/bin/env python3
"""
replay.py -- send crafted/captured writes to a BLE characteristic. This is
the control-PoC core of the toolkit: once CAPTURE_PLAYBOOK.md + btsnoop_att.py
have told you which bytes the Meross app sent to set sensitivity / trigger
auto-calibration, this script replays those bytes (or new variations of them)
against the device, chunking for the negotiated MTU and logging any
notifications that come back.

WARNING: this script CONNECTS to the target and WRITES to it. Only run it
against the MS605 during a supervised session, immediately after the device
owner has pressed the device's physical pairing/config button, and only with
payloads you understand (start with read-only enumerate.py / notify_logger.py
first).

Usage:
    venv/bin/python scripts/replay.py --help
    # MS605 (confirmed): set sensitivity HIGH, then start auto-calibration
    venv/bin/python scripts/replay.py AA:BB:CC:DD:EE:FF \\
        --char 99e7be30-0002-4c6b-98a2-70fcb3471a72 --ms605-tlv 61 03
    venv/bin/python scripts/replay.py AA:BB:CC:DD:EE:FF \\
        --char 99e7be30-0002-4c6b-98a2-70fcb3471a72 --ms605-tlv 52 04 --listen-timeout 200
    # send a prebuilt MS605 frame verbatim, or arbitrary raw bytes
    venv/bin/python scripts/replay.py AA:BB:CC:DD:EE:FF \\
        --char 99e7be30-0002-4c6b-98a2-70fcb3471a72 --file examples/set_sensitivity_high.frame
    venv/bin/python scripts/replay.py AA:BB:CC:DD:EE:FF \\
        --char 99e7be30-0002-4c6b-98a2-70fcb3471a72 --hex 55aac0...aa55
    venv/bin/python scripts/replay.py --self-test
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import ble_common as bc


def build_ms605_attributes(tlv_specs):
    """Turn a list of ['<tag>', '<hexvalue>'] pairs (from --ms605-tlv) into
    (tag, value_bytes) attribute tuples. Tags may be given decimal ('61') or
    hex ('0x3d'); a value of '' means a zero-length attribute."""
    attrs = []
    for tag_str, val_str in tlv_specs:
        try:
            tag = int(tag_str, 0)
        except ValueError as exc:
            raise bc.FrameError(f"invalid --ms605-tlv tag {tag_str!r}: {exc}") from exc
        value = bc.parse_hex_arg(val_str) if val_str.strip() else b""
        attrs.append((tag, value))
    return attrs


def build_payload(args) -> bytes:
    # MS605 TLV construction (mutually exclusive with --hex/--file at the CLI).
    if getattr(args, "ms605_tlv", None):
        attrs = build_ms605_attributes(args.ms605_tlv)
        return bc.build_ms605_tlv_frame(attrs, msg_id=args.ms605_msgid)

    if args.hex is not None:
        payload = bc.parse_hex_arg(args.hex)
    else:
        payload = Path(args.file).read_bytes()

    if args.meross_envelope:
        payload = bc.build_meross_legacy_packet(payload)

    return payload


async def run_replay(address: str, adapter: str | None, connect_timeout: float,
                      char_uuid: str, payload: bytes, chunk_size: int, frame: str,
                      delay_ms: float, response: bool | None, listen: bool,
                      listen_timeout: float) -> tuple[list[bytes], list[tuple[str, bytes]]]:
    from bleak import BleakClient

    kwargs = {}
    if adapter:
        kwargs["bluez"] = {"adapter": adapter}

    received: list[tuple[str, bytes]] = []

    async with BleakClient(address, timeout=connect_timeout, **kwargs) as client:
        effective_chunk_size = chunk_size
        if effective_chunk_size is None:
            mtu = client.mtu_size or 23
            effective_chunk_size = max(1, mtu - 3)
            print(f"Negotiated MTU={mtu}, using chunk_size={effective_chunk_size}", file=sys.stderr)

        target_char = client.services.get_characteristic(char_uuid)
        if target_char is None:
            raise bc.FrameError(f"characteristic {char_uuid} not found on this device "
                                "(run enumerate.py first to confirm the UUID)")

        def make_callback(uuid: str):
            def _cb(_char, data: bytearray) -> None:
                ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                line_hex = bc.to_hex(bytes(data))
                print(f"[{ts}] NOTIFY {uuid} hex={line_hex} ascii={bc.to_ascii(bytes(data))!r}")
                received.append((uuid, bytes(data)))
            return _cb

        subscribed = []
        if listen:
            for service in client.services:
                for char in service.characteristics:
                    if bc.NOTIFY_LIKE_PROPS & set(char.properties):
                        await client.start_notify(char, make_callback(char.uuid))
                        subscribed.append(char)
                        print(f"Listening on {char.uuid} (handle=0x{char.handle:04x})", file=sys.stderr)

        chunks = bc.chunk_bytes(payload, effective_chunk_size, frame=frame)
        print(f"Sending {len(payload)} byte(s) as {len(chunks)} chunk(s) "
              f"(chunk_size={effective_chunk_size}, frame={frame}) to {char_uuid} ...",
              file=sys.stderr)

        write_response = response
        if write_response is None:
            props = set(target_char.properties)
            write_response = "write" in props and "write-without-response" not in props

        for i, chunk in enumerate(chunks):
            await client.write_gatt_char(char_uuid, chunk, response=write_response)
            print(f"  chunk {i + 1}/{len(chunks)}: {bc.to_hex(chunk)}", file=sys.stderr)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

        if listen and listen_timeout > 0:
            print(f"Waiting {listen_timeout:.1f}s for notifications ...", file=sys.stderr)
            await asyncio.sleep(listen_timeout)

        for char in subscribed:
            try:
                await client.stop_notify(char)
            except Exception:
                pass

    return chunks, received


def self_test() -> int:
    """Exercise build_payload()/chunking/envelope logic against synthetic
    inputs -- no adapter, no connection, no real files beyond a throwaway
    temp file created and cleaned up here."""
    import tempfile
    import os
    import types

    # --hex path, no envelope
    args = types.SimpleNamespace(hex="55aa01", file=None, meross_envelope=False)
    payload = build_payload(args)
    assert payload == b"\x55\xaa\x01", payload

    # --file path
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"\x01\x02\x03\x04")
        os.close(fd)
        args = types.SimpleNamespace(hex=None, file=path, meross_envelope=False)
        payload = build_payload(args)
        assert payload == b"\x01\x02\x03\x04", payload
    finally:
        os.remove(path)

    # --meross-envelope wraps and round-trips
    json_bytes = b'{"header":{"method":"GET","namespace":"Appliance.System.All"},"payload":{}}'
    args = types.SimpleNamespace(hex=json_bytes.hex(), file=None, meross_envelope=True)
    framed = build_payload(args)
    assert framed.startswith(b"\x55\xaa")
    assert framed.endswith(b"\xaa\x55")
    parsed = bc.parse_meross_legacy_packet(framed)
    assert parsed.crc_ok
    assert parsed.payload == json_bytes

    # chunking a framed packet larger than a small chunk size, "none" framing
    # (mirrors the real Meross app behavior: app-level envelope + raw split)
    big_json = b'{"header":{},"payload":{"x":"' + b"A" * 500 + b'"}}'
    big_framed = bc.build_meross_legacy_packet(big_json)
    chunks = bc.chunk_bytes(big_framed, 180, frame="none")
    assert len(chunks) > 1
    assert all(len(c) <= 180 for c in chunks)
    reassembled = bc.reassemble_chunks(chunks, frame="none")
    assert reassembled == big_framed
    reparsed = bc.parse_meross_legacy_packet(reassembled)
    assert reparsed.crc_ok
    assert reparsed.payload == big_json

    # --ms605-tlv path: set sensitivity HIGH must reproduce the app's exact
    # frame byte-for-byte (this is the confirmed control-PoC for the MS605).
    args = types.SimpleNamespace(hex=None, file=None, meross_envelope=False,
                                 ms605_tlv=[["61", "03"]], ms605_msgid=1)
    frame = build_payload(args)
    assert frame == bytes.fromhex("55aac00006 1101 3d0001 03 ed58 aa55".replace(" ", "")), frame.hex()

    # --ms605-tlv start auto-calibration (tag 52 = 0x04, SPACE_LEARNING) with a
    # hex-form tag and a custom msgId; parses back correctly.
    args = types.SimpleNamespace(hex=None, file=None, meross_envelope=False,
                                 ms605_tlv=[["0x34", "04"]], ms605_msgid=7)
    frame = build_payload(args)
    parsed_ms605 = bc.parse_ms605_tlv_frame(frame)
    assert parsed_ms605.crc_ok
    assert parsed_ms605.msg_id == 7
    assert parsed_ms605.attributes == [(52, b"\x04")]

    # multi-attribute frame + zero-length value acceptance
    args = types.SimpleNamespace(hex=None, file=None, meross_envelope=False,
                                 ms605_tlv=[["2", "3d"], ["2", "34"]], ms605_msgid=1)
    frame = build_payload(args)
    parsed_multi = bc.parse_ms605_tlv_frame(frame)
    assert parsed_multi.attributes == [(2, b"\x3d"), (2, b"\x34")], parsed_multi.attributes

    # bad tag is rejected as a FrameError (caught at the CLI boundary)
    try:
        build_ms605_attributes([["notanumber", "01"]])
        assert False, "should reject non-numeric tag"
    except bc.FrameError:
        pass

    # unknown characteristic path is exercised in run_replay(), which needs a
    # live client -- covered instead by asserting FrameError is a ValueError
    # subclass with a clear message, so callers can catch it uniformly.
    try:
        raise bc.FrameError("characteristic deadbeef not found")
    except ValueError as exc:
        assert "not found" in str(exc)

    print("ALL replay.py SELF-TESTS PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send crafted/captured writes to a BLE characteristic, chunked for the "
                    "negotiated MTU, logging any notifications received afterward. "
                    "WARNING: this CONNECTS to and WRITES to the target.",
    )
    parser.add_argument("address", nargs="?", default=None,
                         help="target BLE MAC address, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--adapter", "-a", default=None,
                         help="BlueZ adapter name, e.g. hci0 (default: system default adapter)")
    parser.add_argument("--connect-timeout", type=float, default=15.0,
                         help="seconds to wait for the GATT connection (default: 15)")
    parser.add_argument("--char", "-c", default=None,
                         help="target characteristic UUID to write to (required unless --self-test)")

    payload_group = parser.add_mutually_exclusive_group()
    payload_group.add_argument("--hex", default=None,
                                help="payload as hex, e.g. '55aa0004...aa55' (accepts '0x' prefix "
                                    "and ':'/space separators)")
    payload_group.add_argument("--file", default=None,
                                help="path to a binary/text file whose exact bytes are the payload")
    payload_group.add_argument("--ms605-tlv", nargs=2, action="append", default=None,
                                metavar=("TAG", "HEXVALUE"),
                                help="build a CONFIRMED MS605 TLV command frame from one or more "
                                     "TAG HEXVALUE pairs (repeatable), auto-framed as "
                                     "55AA|C0|len|11|msgId|TLVs|CRC16|AA55. TAG is decimal or 0x "
                                     "hex. Examples: --ms605-tlv 61 03 (sensitivity HIGH), "
                                     "--ms605-tlv 52 04 (start auto-calibration/space-learning). "
                                     "Write to the MS605 write char 99E7BE30-0002-...")

    parser.add_argument("--ms605-msgid", type=int, default=1,
                         help="msgId byte for --ms605-tlv frames (1..255; default 1). The device "
                              "echoes it in the response; 0 is reserved for device pushes.")
    parser.add_argument("--meross-envelope", action="store_true",
                         help="wrap the --hex/--file payload in the LEGACY Meross Wi-Fi-device "
                              "packet envelope (55AA + BE16 length + payload + CRC32 + AA55) -- "
                              "this is the Wi-Fi-switch stack, NOT the MS605 (use --ms605-tlv "
                              "for the MS605). See TOOLKIT_README/APK_PROTOCOL")
    parser.add_argument("--chunk-size", type=int, default=None,
                         help="max on-air bytes per BLE write; default: negotiated MTU - 3")
    parser.add_argument("--frame", choices=["none", "seq", "len"], default="none",
                         help="per-chunk framing added on top of MTU-based splitting: "
                              "none=raw split (matches the known Meross app behavior), "
                              "seq=1-byte sequence prefix, len=2-byte BE length prefix "
                              "(default: none)")
    parser.add_argument("--delay-ms", type=float, default=50.0,
                         help="delay between chunk writes in milliseconds (default: 50)")

    response_group = parser.add_mutually_exclusive_group()
    response_group.add_argument("--response", dest="response", action="store_true", default=None,
                                 help="use a write-with-response (default: auto-detect from "
                                     "characteristic properties)")
    response_group.add_argument("--no-response", dest="response", action="store_false",
                                 help="use write-without-response")

    parser.add_argument("--no-listen", dest="listen", action="store_false", default=True,
                         help="don't subscribe to notify/indicate characteristics before writing")
    parser.add_argument("--listen-timeout", type=float, default=3.0,
                         help="seconds to keep listening for notifications after the last "
                              "chunk is sent (default: 3.0)")
    parser.add_argument("--self-test", action="store_true",
                         help="run built-in self-tests against synthetic payloads and exit "
                              "(no adapter or BLE I/O used)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.address:
        parser.error("the following arguments are required: address (unless --self-test)")
    if not args.char:
        parser.error("--char is required (unless --self-test)")
    if args.hex is None and args.file is None and not args.ms605_tlv:
        parser.error("one of --hex, --file, or --ms605-tlv is required (unless --self-test)")
    if args.ms605_tlv and args.meross_envelope:
        parser.error("--ms605-tlv already produces a fully-framed MS605 packet; "
                     "--meross-envelope (a different, legacy framing) cannot be combined with it")
    if args.ms605_tlv and not (0 <= args.ms605_msgid <= 255):
        parser.error("--ms605-msgid must be 0..255")

    try:
        payload = build_payload(args)
    except (bc.HexParseError, bc.FrameError, OSError) as exc:
        parser.error(f"could not build payload: {exc}")
        return 2

    try:
        chunks, received = asyncio.run(run_replay(
            address=args.address,
            adapter=args.adapter,
            connect_timeout=args.connect_timeout,
            char_uuid=args.char,
            payload=payload,
            chunk_size=args.chunk_size,
            frame=args.frame,
            delay_ms=args.delay_ms,
            response=args.response,
            listen=args.listen,
            listen_timeout=args.listen_timeout,
        ))
    except Exception as exc:  # noqa: BLE001 -- top-level CLI error boundary
        print(f"ERROR: replay failed against {args.address}.", file=sys.stderr)
        print(bc.friendly_ble_error(exc, address=args.address), file=sys.stderr)
        return 1

    print(f"\nSent {len(payload)} byte(s) in {len(chunks)} chunk(s). "
          f"Received {len(received)} notification(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
