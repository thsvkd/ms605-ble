#!/usr/bin/env python3
"""
ms605-schedule -- unattended overnight multi-sensor auto-calibration runner.

Built on ms605.driver. Addresses a hardware constraint that neither `ms605`
nor `ms605-driver` can work around by themselves: the MS605 only accepts a
fresh BLE connection for a short window right after its physical button is
pressed. A script launched cold at 2 a.m. (cron/launchd) can never connect --
nobody is there to press the button.

The workaround: **connect while people are still around, hold the links open
with keep-alive pings, and only fire the actual calibration at the scheduled
time** -- the MS605 does not require the button to be pressed again to keep
an already-established link alive, only to accept a *new* one.

Three phases, run once per invocation:
1. Setup (human present)   : walk to each sensor, press its button; this
                              script auto-connects everything that starts
                              advertising and shows up in a scan.
2. Hold  (unattended)      : keep every connected link alive with a periodic
                              bare keep-alive write until --at.
3. Fire  (unattended)      : start auto-calibration (space-learning) on every
                              still-connected sensor concurrently, then
                              disconnect everything so each sensor just runs
                              on its freshly learned baseline.

A sensor whose link drops during the Hold phase is marked lost and skipped --
there is no way to recover it without a human pressing its button again, so
this script does not attempt to reconnect unattended.

Usage
-----
    ms605-schedule --at 02:00
    ms605-schedule --at 02:00 --log-file night.log

Run `ms605-schedule --help` for all options.

Caveat: the multi-hour keep-alive hold (phase 2) reuses the same bare
keep-alive frame validated during ~3-minute calibration windows, but holding
a link for hours has not itself been captured/verified against a real
device. Treat the first overnight run as a trial and check the log the next
morning.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ms605 import MS605, MS605ConnectionError, MS605Error, MS605TimeoutError
from ms605.discovery import adapter_status_summary, friendly_ble_error


# ---------------------------------------------------------------------------
# small async helpers (mirrors ms605.cli._shared.ainput; not imported from
# there because scheduled_calibrate manages several devices at once and does
# not use the single-device LiveLink/discover_and_select/connect_with_retry flow)
# ---------------------------------------------------------------------------
async def ainput(prompt: str = "") -> str:
    """Non-blocking input() so the event loop (scan/connect tasks) keeps running."""
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, sys.stdin.readline)).rstrip("\n") if not prompt \
        else (await loop.run_in_executor(None, _prompt, prompt))


def _prompt(text: str) -> str:
    try:
        return input(text)
    except EOFError:
        return ""


def ts() -> str:
    return time.strftime("%H:%M:%S")


def _format_hms(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# logging (stdout + optional --log-file tee)
# ---------------------------------------------------------------------------
_log_file = None  # type: Optional[object]


def log(msg: str = "") -> None:
    print(msg)
    if _log_file is not None:
        print(msg, file=_log_file)
        _log_file.flush()


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------
def resolve_target_datetime(at: str, now: datetime) -> datetime:
    """Resolve `--at HH:MM` to a concrete datetime relative to `now`.

    If that time of day has already passed today (or is exactly now), rolls
    over to tomorrow -- the intended use is "set up in the evening, fire
    after everyone has left, possibly past midnight"."""
    try:
        hh_str, mm_str = at.strip().split(":", 1)
        hh, mm = int(hh_str), int(mm_str)
    except ValueError as exc:
        raise ValueError(f"--at 형식이 잘못됨: {at!r} (HH:MM 24시간제)") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"--at 시각 범위 오류: {at!r} (00:00~23:59)")
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ---------------------------------------------------------------------------
# per-device state
# ---------------------------------------------------------------------------
_STATUS_LABEL = {
    "connected": "미보정 (연결 유지된 채 종료)",
    "lost": "연결 끊김 (대기 중 유실 -- 버튼 재입력 필요)",
    "calibrated_ok": "보정 성공",
    "calibrated_fail": "보정 실패",
    "calibration_timeout": "보정 결과 타임아웃",
    "calibration_lost": "보정 중 연결 끊김",
    "calibration_error": "보정 오류",
}


@dataclass
class ManagedDevice:
    ms: MS605
    device: object
    name: str
    address: str
    status: str = "connected"
    detail: str = ""


def format_summary(devices: Sequence[ManagedDevice]) -> str:
    lines = ["\n" + "=" * 60, " 최종 결과", "=" * 60]
    if not devices:
        lines.append("연결된 센서가 없었습니다.")
    for md in devices:
        label = _STATUS_LABEL.get(md.status, md.status)
        detail = f" ({md.detail})" if md.detail else ""
        lines.append(f"  - {md.name:<24} {md.address}  -> {label}{detail}")
    ok_n = sum(1 for md in devices if md.status == "calibrated_ok")
    lines.append(f"\n{ok_n}/{len(devices)}대 보정 성공.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# phase 1: SETUP -- human present, press each sensor's button once
# ---------------------------------------------------------------------------
async def phase_setup(scan_secs: float, connect_timeout: float) -> list[ManagedDevice]:
    pool: dict = {}
    attempting: set = set()
    pending_tasks: set = set()
    stop = asyncio.Event()

    async def try_connect(dev) -> None:
        addr = dev.address
        name = dev.name or "(이름없음)"
        log(f"\n[{ts()}] 🔗 새 기기 발견, 연결 시도: {name} {addr}")
        ms = MS605(dev)
        try:
            await ms.connect(timeout=connect_timeout)
            pool[addr] = ManagedDevice(ms=ms, device=dev, name=name, address=addr)
            log(f"[{ts()}] ✅ 연결됨 (누적 {len(pool)}대): {name} {addr}")
        except Exception as exc:  # noqa: BLE001 - surface any BLE failure, keep scanning
            log(f"[{ts()}]    ✗ {name} {addr} 연결 실패 (버튼을 방금 눌렀는지 확인): "
                f"{friendly_ble_error(exc, addr)}")
        finally:
            attempting.discard(addr)

    async def scanner() -> None:
        while not stop.is_set():
            try:
                found = await MS605.scan(timeout=scan_secs)
            except Exception as exc:  # noqa: BLE001
                log(f"  (스캔 오류: {exc})")
                found = []
            for dev in found:
                addr = dev.address
                if addr in pool or addr in attempting:
                    continue
                attempting.add(addr)
                t = asyncio.create_task(try_connect(dev))
                pending_tasks.add(t)
                t.add_done_callback(pending_tasks.discard)
            await asyncio.sleep(1.0)

    scan_task = asyncio.create_task(scanner())
    log("\n" + "=" * 60)
    log(" 설정 단계 -- 각 센서 버튼을 순서대로 눌러 연결하세요")
    log("=" * 60)
    log("주의: BLE는 한 번에 하나만 연결됩니다. 각 센서에서 Meross 앱이")
    log("      연결 중이면 먼저 종료/백그라운드 처리하세요.")
    log("버튼을 누른 센서가 스캔에 잡히는 대로 자동으로 연결합니다.")
    log("모든 센서를 연결했으면 Enter 를 눌러 다음 단계(대기)로 진행하세요.\n")
    await ainput("연결 완료 후 Enter 입력: ")

    stop.set()
    scan_task.cancel()
    try:
        await scan_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    if pending_tasks:
        log(f"   (진행 중인 연결 시도 {len(pending_tasks)}건 마무리 대기...)")
        await asyncio.gather(*pending_tasks, return_exceptions=True)

    devices = list(pool.values())
    log(f"\n설정 완료 -- 총 {len(devices)}대 연결됨.")
    for md in devices:
        log(f"   - {md.name} {md.address}")
    return devices


# ---------------------------------------------------------------------------
# phase 2: HOLD -- unattended, keep every link alive until --at
# ---------------------------------------------------------------------------
async def phase_hold(devices: list[ManagedDevice], target: datetime, interval: float) -> None:
    log("\n" + "=" * 60)
    log(f" 대기 단계 -- {target.strftime('%Y-%m-%d %H:%M')} 까지 {len(devices)}대 연결 유지")
    log("=" * 60)

    async def hold_one(md: ManagedDevice) -> None:
        while True:
            now = datetime.now()
            if now >= target:
                return
            await asyncio.sleep(min(interval, (target - now).total_seconds()))
            if datetime.now() >= target:
                return
            try:
                await md.ms.ping()
            except Exception as exc:  # noqa: BLE001 - link is gone, cannot recover unattended
                md.status = "lost"
                md.detail = str(exc)
                log(f"\n[{ts()}] ⚠️  {md.name} {md.address} 연결 끊김 (대기 중, 버튼 재입력 불가): {exc}")
                return

    async def heartbeat() -> None:
        beat = max(interval, 60.0)
        while datetime.now() < target:
            await asyncio.sleep(min(beat, max(1.0, (target - datetime.now()).total_seconds())))
            if datetime.now() >= target:
                return
            alive = sum(1 for md in devices if md.status == "connected")
            left = target - datetime.now()
            log(f"  ... [{ts()}] 대기 중: {alive}/{len(devices)}대 연결 유지, 발사까지 {_format_hms(left)}")

    hb = asyncio.create_task(heartbeat())
    try:
        await asyncio.gather(*(hold_one(md) for md in devices))
    finally:
        hb.cancel()
        try:
            await hb
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    left = (target - datetime.now()).total_seconds()
    if left > 0:
        await asyncio.sleep(left)


# ---------------------------------------------------------------------------
# phase 3: FIRE -- unattended, calibrate every still-connected sensor
# ---------------------------------------------------------------------------
async def phase_fire(devices: list[ManagedDevice], calibration_timeout: float) -> None:
    live = [md for md in devices if md.status == "connected"]
    log("\n" + "=" * 60)
    log(f" 발사 -- {len(live)}/{len(devices)}대 자동 보정 시작")
    log("=" * 60)
    if not live:
        log("연결 유지된 센서가 없어 보정을 건너뜁니다.")
        return

    async def fire_one(md: ManagedDevice) -> None:
        log(f"\n[{ts()}] 🔄 {md.name} {md.address} 자동 보정 시작...")
        try:
            ok = await md.ms.start_auto_calibration(timeout=calibration_timeout)
            md.status = "calibrated_ok" if ok else "calibrated_fail"
            mark = "✅" if ok else "❌"
            log(f"[{ts()}] {mark} {md.name} {md.address} 보정 {'성공' if ok else '실패'}")
        except MS605TimeoutError:
            md.status = "calibration_timeout"
            log(f"[{ts()}] ⏱️  {md.name} {md.address} 보정 결과 타임아웃")
        except MS605ConnectionError as exc:
            md.status = "calibration_lost"
            md.detail = str(exc)
            log(f"[{ts()}] 🔌 {md.name} {md.address} 보정 중 연결 끊김: {exc}")
        except MS605Error as exc:
            md.status = "calibration_error"
            md.detail = str(exc)
            log(f"[{ts()}] ❌ {md.name} {md.address} 보정 오류: {exc}")

    await asyncio.gather(*(fire_one(md) for md in live))


# ---------------------------------------------------------------------------
# phase 4: RELEASE -- always disconnect everything, bounded
# ---------------------------------------------------------------------------
async def phase_release(devices: list[ManagedDevice]) -> None:
    if not devices:
        return
    log("\n" + "=" * 60)
    log(" 연결 해제")
    log("=" * 60)

    async def release_one(md: ManagedDevice) -> None:
        try:
            await asyncio.wait_for(md.ms.disconnect(), timeout=5.0)
        except BaseException:  # noqa: BLE001 - best-effort cleanup, never hang here
            pass

    await asyncio.gather(*(release_one(md) for md in devices))


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ms605-schedule",
        description=(
            "여러 MS605 센서를 낮 동안(사람이 있을 때) 붙잡아 연결을 유지하다가, 지정한 "
            "시각(예: 새벽)에 한꺼번에 자동 보정(공간 학습)을 실행하고 연결을 해제하는 "
            "무인 야간 보정 러너.\n\n"
            "MS605는 물리 버튼을 누른 직후 짧은 시간만 새 연결을 받으므로, 이 스크립트는 "
            "사람이 있는 동안 각 센서 버튼을 눌러 연결을 맺어두고(설정 단계), 그 뒤로는 "
            "keep-alive만으로 링크를 물고 있다가(대기 단계), --at 시각에 전체 센서에서 "
            "동시에 보정을 실행합니다(발사 단계). 대기 중 링크가 끊긴 센서는 버튼을 다시 "
            "눌러줄 사람이 없으므로 재연결을 시도하지 않고 최종 결과에 '연결 끊김'으로 "
            "표시됩니다."
        ),
        epilog=(
            "예시\n"
            "  ms605-schedule --at 02:00\n"
            "  ms605-schedule --at 02:00 --log-file night.log\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--at", metavar="HH:MM", default=None,
        help="보정을 실행할 시각(24시간제, 로컬 시간). 이미 지난 시각이면 다음날로 이월. 필수.",
    )
    ap.add_argument("--scan-secs", type=float, default=6.0,
                     help="설정 단계 BLE 스캔 시간(초), 기본 6")
    ap.add_argument("--connect-timeout", type=float, default=12.0,
                     help="연결 타임아웃(초), 기본 12")
    ap.add_argument("--keepalive-interval", type=float, default=15.0,
                     help="대기 단계 keep-alive 간격(초), 기본 15 "
                          "(기기의 유휴 연결끊김 임계치보다 짧아야 함)")
    ap.add_argument("--calibration-timeout", type=float, default=200.0,
                     help="자동 보정 결과(tag62) 대기 타임아웃(초), 기본 200 (~3분 + 여유)")
    ap.add_argument("--log-file", metavar="PATH", default=None,
                     help="모든 출력을 이 파일에도 추가 기록 (무인 실행 다음날 확인용)")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    global _log_file
    args = build_parser().parse_args(argv)

    if not args.at:
        build_parser().error("--at HH:MM 이 필요합니다")

    now = datetime.now()
    try:
        target = resolve_target_datetime(args.at, now)
    except ValueError as exc:
        build_parser().error(str(exc))

    if args.log_file:
        _log_file = open(args.log_file, "a", encoding="utf-8")

    async def run() -> int:
        log("=" * 60)
        log(" MS605 무인 야간 자동 보정 러너")
        log("=" * 60)
        log(adapter_status_summary())
        log(f"목표 시각: {target.strftime('%Y-%m-%d %H:%M')} (지금: {now.strftime('%Y-%m-%d %H:%M')})")

        devices: list[ManagedDevice] = []
        try:
            devices = await phase_setup(args.scan_secs, args.connect_timeout)
            if not devices:
                log("\n연결된 센서가 없어 종료합니다.")
                return 1
            await phase_hold(devices, target, args.keepalive_interval)
            await phase_fire(devices, args.calibration_timeout)
        finally:
            if devices:
                await phase_release(devices)
        log(format_summary(devices))
        return 0 if devices and all(md.status == "calibrated_ok" for md in devices) else 1

    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        log("\n중단되었습니다.")
        code = 130

    if _log_file is not None:
        _log_file.close()
    # see ms605.cli.cli's main() for why: ainput() blocks a worker thread on a
    # real readline() that Ctrl-C cannot cancel, so a normal interpreter exit
    # can hang waiting to join it. Cleanup already ran above; hard-exit.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
