#!/usr/bin/env python3
"""ms605_debug.py — instrumented zone-threshold write diagnosis (collaborative).

Why: writing tag51 (zone thresholds) returns a status=0 ack, yet reading the
config straight back shows the *old* values. This tool logs every raw BLE frame
(TX / RX / unsolicited push) with relative timestamps and runs controlled
experiments to tell the competing hypotheses apart:

  EXP-A  write tag51 ALONE, then re-read at increasing delays.
         → if a delayed read shows the new values, it's a timing/apply lag.
         → any push arriving after the write is logged (event/ack-of-apply?).
  EXP-B  write tag61=4 (sensitivity CUSTOM) + tag51 in ONE frame, then re-read.
         → if this sticks but EXP-A didn't, the device only honors custom
           thresholds when CUSTOM is (re)asserted in the same write.

At the end it best-effort restores the original values.

Run:
    uv run python ms605_debug.py
    # (press the sensor button when prompted, like the PoC)

Paste the ENTIRE output back so we can read the raw frames together.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import ms605_ble as drv
from ms605_ble import (
    MS605,
    TAG_SENSITIVITY,
    TAG_ZONE_THRESHOLDS,
    _encode_zone_thresholds,
    parse_frame,
)
from ms605_poc import LiveLink, connect_with_retry, discover_and_select

_T0 = time.monotonic()


def _ts() -> str:
    return f"{time.monotonic() - _T0:8.3f}s"


def _thr_from_tag51(value: bytes | None):
    """Decode a 28-byte tag51 payload into 7 (trigger, maintain) pairs."""
    if not value or len(value) != 28:
        return None
    return [
        (
            int.from_bytes(value[i : i + 2], "big"),
            int.from_bytes(value[i + 2 : i + 4], "big"),
        )
        for i in range(0, 28, 4)
    ]


def install_sniffer(ms: MS605) -> None:
    """Monkeypatch the instance to log full frames on the way out and in.

    We wrap `_write_chunks` (receives the complete TLV frame before chunking)
    and `_dispatch` (receives every fully-parsed inbound frame, response OR
    push). Behaviour is unchanged — we only print then delegate."""
    orig_write = ms._write_chunks
    orig_dispatch = ms._dispatch

    async def logged_write(frame: bytes) -> None:
        try:
            p = parse_frame(frame)
            desc = ", ".join(f"tag{t}={v.hex()}" for t, v in p.attributes)
            print(f"[{_ts()}] TX   msgId={p.msg_id:<3} {desc}")
            thr = _thr_from_tag51(p.get(TAG_ZONE_THRESHOLDS))
            if thr:
                print(f"                    └ tag51 WRITE thresholds={thr}")
        except Exception:  # noqa: BLE001 - fall back to raw hex
            print(f"[{_ts()}] TX   raw={frame.hex()}")
        await orig_write(frame)

    def logged_dispatch(parsed) -> None:
        kind = "PUSH" if parsed.is_push else "RX  "
        desc = ", ".join(f"tag{t}={v.hex()}" for t, v in parsed.attributes)
        print(
            f"[{_ts()}] {kind} msgId={parsed.msg_id:<3} "
            f"crc_ok={parsed.crc_ok} status={parsed.status()}  {desc}"
        )
        thr = _thr_from_tag51(parsed.get(TAG_ZONE_THRESHOLDS))
        if thr:
            print(f"                    └ tag51 READ thresholds={thr}")
        orig_dispatch(parsed)

    ms._write_chunks = logged_write  # type: ignore[method-assign]
    ms._dispatch = logged_dispatch  # type: ignore[method-assign]


async def read_thresholds(ms: MS605, label: str):
    cfg = await ms.read_config()
    thr = [(z.trigger, z.maintain) for z in cfg.zone_thresholds]
    print(f"  → [{label}] sens(tag61)={cfg.sensitivity}  thresholds={thr}")
    return thr, cfg.sensitivity


async def poll_readbacks(ms: MS605, delays):
    """Re-read config after each cumulative delay; return [(delay, thr, sens)]."""
    out = []
    for d in delays:
        if d > 0:
            await asyncio.sleep(d)
        thr, sens = await read_thresholds(ms, f"read @+{d}s")
        out.append((d, thr, sens))
    return out


async def experiment_A(link: LiveLink):
    ms = link.ms
    print("\n" + "=" * 72)
    print(" EXP-A: write tag51 ALONE → poll re-reads (delay + event hypotheses)")
    print("=" * 72)
    await link.ensure()
    base, _ = await read_thresholds(ms, "baseline")

    sentinel = [(201 + i, 101 + i) for i in range(7)]  # distinctive, in-range
    print(f"\n  writing sentinel = {sentinel}")
    await link.ensure()
    await ms.set_zone_thresholds(sentinel)
    print("  (ack received above; watching pushes + polling re-reads)\n")

    reads = await poll_readbacks(ms, [0, 0.5, 1.0, 2.0, 4.0])
    matched = [d for d, thr, _ in reads if thr == sentinel]
    print(f"\n  >>> EXP-A verdict: sentinel visible on read-back at delays "
          f"{matched if matched else 'NEVER'}")
    return base, sentinel, reads


async def experiment_B(link: LiveLink):
    ms = link.ms
    print("\n" + "=" * 72)
    print(" EXP-B: write tag61=4 (CUSTOM) + tag51 in ONE frame → re-read")
    print("=" * 72)
    sentinel = [(211 + i, 111 + i) for i in range(7)]
    value = _encode_zone_thresholds(sentinel)
    print(f"  writing (single frame) tag61=04 + tag51 sentinel = {sentinel}")
    await link.ensure()
    # one command frame carrying BOTH attributes (mirrors an app "apply custom")
    await ms._send([(TAG_SENSITIVITY, bytes([4])), (TAG_ZONE_THRESHOLDS, value)])

    reads = await poll_readbacks(ms, [0, 1.0, 3.0])
    matched = [d for d, thr, _ in reads if thr == sentinel]
    print(f"\n  >>> EXP-B verdict: sentinel visible on read-back at delays "
          f"{matched if matched else 'NEVER'}")
    return sentinel, reads


async def run() -> None:
    print("=" * 72)
    print(" MS605 zone-threshold write debugger")
    print("=" * 72)
    print("주의: 실제 센서에 테스트용 임계값을 씁니다. 끝나면 원래 값 복원을 시도합니다.\n")

    device = await discover_and_select(6.0)
    ms = await connect_with_retry(device, 6.0, 12.0)
    install_sniffer(ms)
    link = LiveLink(ms, device, 6.0, 12.0)

    try:
        base, sent_a, reads_a = await experiment_A(link)

        if any(thr == sent_a for _, thr, _ in reads_a):
            print("\nEXP-A가 이미 반영됨 → EXP-B는 생략합니다.")
        else:
            await experiment_B(link)

        print("\n원래 값 복원 시도...")
        await link.ensure()
        await ms.set_zone_thresholds(base)
        await asyncio.sleep(1.0)
        await read_thresholds(ms, "after restore")
    finally:
        print("\n연결 종료...")
        try:
            await asyncio.wait_for(ms.disconnect(), timeout=5.0)
        except BaseException:  # noqa: BLE001 - best-effort cleanup
            pass

    print("\n디버그 종료. 위 로그 전체를 복사해서 보내주세요.")


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
