#!/usr/bin/env python3
"""
ms605 -- interactive control app for the Meross MS605 presence sensor.

Cross-platform (Linux / macOS / Windows) via `bleak`. A thin terminal front end
over the ms605 core (session / calibration / fleet / storage): this module only
renders and prompts; every device operation goes through the core.

No Meross account or device registration is needed for the BLE config channel:
the only real gates are (1) a physical button press to make the sensor
connectable and (2) BLE is single-link, so **close/kill the Meross app first**
(only one central can be connected at a time).

Flows
-----
1. Connection : scan -> pick a device -> connect, with an automatic
   "press the button" retry loop that keeps trying in the background until
   the sensor becomes connectable.
2. Auto-calibration : after connecting, wait for an explicit Enter (space should
   already be cleared) before triggering space-learning; show the live radar
   activity in real time; on success, print + save the committed thresholds to
   cal_results/calibration_history.jsonl (under the data dir; see
   ms605.storage.data_root).
3. Detailed adjustment : view and set the per-distance **Presence Trigger** and
   **Presence Maintain** thresholds (the two adjustment modes).
4. Live monitor : continuously show PIR / per-zone radar / host-computed
   presence-or-absence / per-zone trigger state, all straight from device pushes.
5. Zone & sub-sensor config : enable/disable individual zones, assign zones to
   Sensor1/2/3, and set each sub-sensor's presence/absence duration.

The interactive menu drives a single device (flow 1's connect gives one link).
The `calibrate` subcommand is different: it connects *several* sensors at once
and batch auto-calibrates them together (status/result only, no live radar) --
see run_batch_calibration below. It absorbs the former `ms605-schedule` runner,
whose scheduled-fire behaviour is now `calibrate --schedule HH:MM`.

Usage
-----
    ms605                    # interactive menu (single device)
    ms605 calibrate          # connect several sensors, batch auto-calibrate now
    ms605 calibrate --collect        # gather sensors as you press each button
    ms605 calibrate --schedule 02:00 # hold links, fire unattended at 02:00
    ms605 clone              # read one sensor's settings, copy onto others
    ms605 clone --save private-profiles/profile.json --no-apply
    ms605 clone --from-file private-profiles/profile.json
    ms605 read               # one-shot: print current config
    ms605 set-zone 95,40 85,40 75,40 60,40 55,40 40,35 35,28
    ms605 set-sensitivity 3  # 1=LOW 2=MED 3=HIGH 4=CUSTOM
    ms605 read-dnd           # one-shot: read do-not-disturb state
    ms605 set-dnd 1          # one-shot: turn do-not-disturb on
    ms605 read-pir           # one-shot: read PIR state
    ms605 read-subsensor-status
    ms605 sync-time          # write the host's UTC time to the device
    ms605 read-history presence
    ms605 --address <UUID> calibrate   # target one device
    ms605 --scan-secs 8 read # longer scans

Run `ms605 --help` for the full list of subcommands and options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ms605 import (
    ConfigProfile,
    DetectMode,
    FrameError,
    MS605Config,
    MS605Error,
    Sensitivity,
    StorageError,
)
from ms605.calibration import CalibrationJob
from ms605.cli import _ui
from ms605.cli._shared import (
    _fmt_device_choice,
    ainput,
    connect_with_retry,
    discover_and_select,
    ensure,
)
from ms605.discovery import adapter_status_summary, friendly_ble_error
from ms605.events import (
    ApplyResult,
    ApplyStatus,
    BatchChanged,
    BatchState,
    BusyChanged,
    CalibrationProgress,
    CalibrationResult,
    CalibrationState,
    CalibrationStateChanged,
    Event,
    GatherFailed,
    KeepAliveMissed,
    LinkState,
    LinkStateChanged,
    LiveRadar,
    PirChanged,
    SensorGathered,
)
from ms605.fleet import Draft, Fleet, SensorChanges, ThresholdChange, apply_changes
from ms605.gui.cli import add_parser as add_gui_parser
from ms605.gui.cli import run_gui
from ms605.models import (
    FALLBACK_DISTANCES_M,
    PROFILE_SECTION_KEYS,
    PROFILE_SECTION_LABELS,
    RadarZoneLive,
    zone_distances,
)
from ms605.registry import Registry
from ms605.session import DeviceInfo, DeviceSession
from ms605.storage import Storage

THRESHOLD_MIN, THRESHOLD_MAX = 0, 500  # raw radar-energy range (app axis)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def format_config_table(cfg: MS605Config, *, summary: bool = True) -> str:
    dists = zone_distances(cfg)
    lines: list[str] = []
    if summary:
        try:
            sens_name = Sensitivity(cfg.sensitivity).name
        except ValueError:
            sens_name = f"?({cfg.sensitivity})"
        try:
            mode_name = DetectMode(cfg.detect_mode).name
        except ValueError:
            mode_name = f"?({cfg.detect_mode})"
        lines += [
            f"  민감도(tag61)      : {cfg.sensitivity} ({sens_name})",
            f"  감지모드(tag52)    : {cfg.detect_mode} ({mode_name})",
            f"  존 활성화(tag50)   : {''.join('1' if b else '0' for b in cfg.zones_enabled())}",
            "",
        ]
    lines += [
        "  #  거리      Presence Trigger   Presence Maintain",
        "  -- --------  ----------------   -----------------",
    ]
    for i, zt in enumerate(cfg.zone_thresholds):
        dist = f"{dists[i]:.1f} m" if i < len(dists) else "   ?  "
        lines.append(f"  {i}  {dist:>7}   {zt.trigger:>10}        {zt.maintain:>10}")
    return "\n".join(lines)


def merge_thresholds(
    cfg: MS605Config, triggers: Sequence[int], maintains: Sequence[int]
) -> list[tuple[int, int]]:
    """Build the 7 (trigger, maintain) pairs to write, taking edited columns
    where provided and keeping the current value elsewhere."""
    cur = list(cfg.zone_thresholds)
    pairs: list[tuple[int, int]] = []
    for i in range(len(cur)):
        t = triggers[i] if i < len(triggers) and triggers[i] is not None else cur[i].trigger
        m = maintains[i] if i < len(maintains) and maintains[i] is not None else cur[i].maintain
        pairs.append((int(t), int(m)))
    return pairs


# ---------------------------------------------------------------------------
# device info / header
# ---------------------------------------------------------------------------
def _fmt_device_id(info: DeviceInfo | None) -> str:
    return info.device_id if info else "?"


def _fmt_version(info: DeviceInfo | None) -> str:
    return ".".join(str(b) for b in info.version) if info and info.version else "?"


def _fmt_battery(info: DeviceInfo | None) -> str:
    return f"{info.battery_pct}%" if info and info.battery_pct is not None else "?"


def _fmt_light(info: DeviceInfo | None) -> str:
    return f"{info.light_lux} lux" if info and info.light_lux is not None else "?"


def format_device_header(name: str | None, address: str, cfg: MS605Config, info: DeviceInfo | None) -> str:
    try:
        sens = f"{cfg.sensitivity} ({Sensitivity(cfg.sensitivity).name})"
    except ValueError:
        sens = str(cfg.sensitivity)
    try:
        mode = f"{cfg.detect_mode} ({DetectMode(cfg.detect_mode).name})"
    except ValueError:
        mode = str(cfg.detect_mode)
    bar = "=" * 60
    return "\n".join([
        bar,
        f" Meross MS605  —  {name or '(이름없음)'}  [{address}]",
        bar,
        f"  Device ID       : {_fmt_device_id(info)}",
        f"  펌웨어(tag21)    : {_fmt_version(info)}  (supported-tags version)",
        f"  배터리(tag23)    : {_fmt_battery(info)}",
        f"  조도(tag36)      : {_fmt_light(info)}",
        f"  민감도(tag61)    : {sens}",
        f"  감지모드(tag52)  : {mode}",
        f"  존 활성화(tag50) : {''.join('1' if b else '0' for b in cfg.zones_enabled())}",
        bar,
        format_config_table(cfg, summary=False),
        bar,
    ])


async def _read_config(session: DeviceSession) -> MS605Config:
    async with session.operation("read") as ms:
        return await ms.read_config()


async def print_device_header(session: DeviceSession) -> None:
    with _ui.status("디바이스 정보를 읽는 중..."):
        cfg = await _read_config(session)
        try:
            info: DeviceInfo | None = await session.read_info()
        except MS605Error:  # the header still prints, with "?" for the unread tags
            info = None
    _ui.console.print(format_device_header(session.name, session.address, cfg, info))


def _absolute_thresholds(pairs: Sequence[tuple[int, int]]) -> ThresholdChange:
    return ThresholdChange(relative=False, trigger=[t for t, _ in pairs], maintain=[m for _, m in pairs])


async def _apply(session: DeviceSession, changes: SensorChanges) -> bool:
    """Write `changes` through the core pipeline (snapshot -> write -> polled
    verify). Returns whether the read-back confirmed every written section.
    Raises MS605Error if the write failed or could not be read back at all,
    like the direct driver call it replaces."""
    if session.device_id is None:
        await session.read_info()  # snapshots are kept per device id
    result = await apply_changes(session, changes, Storage())
    if result.status in (ApplyStatus.FAILED, ApplyStatus.UNVERIFIED):
        raise MS605Error(result.error or result.status.value)
    return result.status is ApplyStatus.OK


# ===========================================================================
# FLOW 1: AUTO-CALIBRATION (real-time)
# ===========================================================================
def _format_radar_zone(zone: RadarZoneLive) -> str:
    """Render one zone's tag-55 snapshot as 'Z{i}:current/threshold', with a
    trailing '*' when the device itself reports the zone as trigger-active
    (segTriggerStatus). Both numbers come straight from the device -- nothing
    here is computed on the host."""
    if not zone.enabled:
        return f"Z{zone.index}:off"
    mark = "*" if zone.trigger_active else " "
    return f"Z{zone.index}:{zone.current_trigger:>3}/{zone.trigger_threshold:<3}{mark}"


async def _await_calibration_trigger(session: DeviceSession) -> None:
    """Gate the actual SPACE_LEARNING trigger behind an explicit confirmation
    -- being connected doesn't mean the space is empty yet, and the app's own
    flow starts with "clear the space of people" (step 1). The trigger fires
    the instant Enter is pressed, with no further countdown: the expectation
    is the operator already left the space before pressing it.

    Waiting for the operator's Enter can take an unbounded amount of time; the
    session's keep-alive holds the link meanwhile, and the link is
    re-established afterwards (ensure()) in case it still dropped, before the
    caller ever writes tag52."""
    print("보정은 감지 공간에 사람이 없는 상태에서 시작해야 합니다.")
    await ainput("공간을 비운 뒤 준비되면 Enter를 눌러 보정을 즉시 트리거하세요: ")
    await ensure(session)  # re-establish if the wait above still lost the link
    print("🚀 보정 명령을 전송합니다.\n")


async def flow_auto_calibration(session: DeviceSession) -> None:
    await ensure(session)  # guarantee a live link (no-op if already connected)
    _ui.header("자동 보정 (공간 학습 / Auto-Adjust)")
    _ui.muted("디바이스가 주변 환경을 학습합니다. 학습 중에는 감지 공간에서")
    _ui.muted("사람이 평소처럼 움직이거나 자리를 비워주세요. (최대 ~3분)")
    _ui.muted("※ 임계값 계산/조정은 전부 기기 내부에서 이뤄집니다 — 호스트가 값을 계산/쓰지 않습니다 (앱과 동일).")

    await _await_calibration_trigger(session)

    prev_presence: bool | None = None

    # Entering SPACE_LEARNING (tag52=4) makes the device stream tag55/56 on its
    # own; like the app, no live-output (tag54) write is needed here.
    def on_event(ev: Event) -> None:
        nonlocal prev_presence
        ts = time.strftime("%H:%M:%S")
        if isinstance(ev, LiveRadar):
            snap = ev.snapshot
            zones = " ".join(_format_radar_zone(z) for z in snap.zones)
            print(f"  [{ts}] {zones}")
            presence_now = snap.sub_sensor_presence[0] if snap.sub_sensor_presence else None
            if presence_now is not None and presence_now != prev_presence:
                if prev_presence is not None:
                    label = "🟢 재실 감지(presence=True)" if presence_now else "🔴 부재 전환(presence=False)"
                    print(f"  [{ts}] 기기 판정 전환: {label}")
                prev_presence = presence_now
        elif isinstance(ev, PirChanged):
            print(f"  [{ts}] PIR 상태: {'감지' if ev.detected else '없음'}")
        elif isinstance(ev, CalibrationProgress):
            print(f"  ... 학습 진행 중 (경과 {int(ev.elapsed_s)}s)")
        elif isinstance(ev, CalibrationResult):
            ok = ev.state is CalibrationState.SUCCEEDED
            if ok or (ev.state is CalibrationState.FAILED and ev.error is None):  # a tag62 result arrived
                print(f"  [{ts}] ★ 보정 결과 수신: {'성공' if ok else '실패'}")

    storage = Storage()
    job = CalibrationJob(session, storage=storage, progress_interval=5.0)
    print("🔄 자동 보정 시작... (앱과 동일하게 ~15s마다 keep-alive 로 링크를 유지합니다)\n")
    unsubscribe = session.bus.subscribe(on_event)
    try:
        result = await job.run()
    finally:
        # the same session outlives this flow (menu / live monitor): a handler
        # left behind would keep printing and stack up on every re-run.
        unsubscribe()

    state = result.state
    if state is CalibrationState.TIMEOUT:
        print("\n⏱️  3분 내 보정 결과를 받지 못했습니다 (타임아웃).")
    elif state is CalibrationState.LOST:
        # the device dropped the link and learning resets on disconnect, so the
        # run must be restarted from a fresh connection.
        print(f"\n🔌 보정 중 BLE 연결이 끊어졌습니다: {result.error or result.detail}")
        print("   기기 버튼을 다시 누른 뒤 메뉴 [1]로 재시도해주세요.")
    elif state is CalibrationState.FAILED and result.error is not None:
        print(f"\n오류: {result.error}")

    print("\n" + "-" * 60)
    if state is CalibrationState.SUCCEEDED:
        print("✅ 자동 보정 완료 — 성공적으로 학습되었습니다.")
        if result.after is None:  # the job could not re-read what was committed
            reason = result.detail.removeprefix("반영값 재조회 실패: ")
            print(f"(반영값 재조회 실패 — 메뉴 [3] 설정읽기로 확인하세요: {reason})")
        else:
            print("\n반영된 존별 임계값 (tag51 재조회):")
            print(format_config_table(job.config_after, summary=False))
            if result.history_saved:
                print(f"💾 보정 결과 저장됨: {storage.history_path}")
            else:
                print(f"⚠️  보정 결과 저장 실패: {result.detail.removeprefix('결과 저장 실패: ')}")
    elif state is CalibrationState.LOST:
        print("⚠️  자동 보정 미완료 — 연결 끊김으로 중단되었습니다. 재연결 후 다시 시도하세요.")
    else:
        print("❌ 자동 보정 완료 — 실패 또는 미완료. 환경을 확인하고 다시 시도하세요.")
    print("-" * 60)


# ===========================================================================
# MULTI-SENSOR BATCH CALIBRATION  (the `calibrate` subcommand)
#
# Absorbs the former standalone `ms605-schedule` runner: connect several
# sensors at once and auto-calibrate them together. There are two ways to
# gather sensors and two ways to fire:
#
#   gather : default    -> scan once, show a numbered menu, pick several
#                          devices (or all), then connect each.
#            --collect  -> keep scanning and connect each sensor the instant it
#                          advertises (walk around pressing buttons); Enter ends
#                          the collection. This is the hardware-robust path --
#                          the MS605 only accepts a connection briefly after its
#                          button is pressed, so a scan-then-menu can miss the
#                          window on the sensors whose buttons were pressed first.
#            --address  -> target one specific device (a batch of one).
#   fire   : default    -> immediate: clear the space, press Enter, fire now
#                          (links are kept alive with pings during the wait).
#            --schedule HH:MM -> hold every link alive until that clock time,
#                          then fire unattended (the old overnight behaviour).
#
# The devices live in an ms605.fleet.Fleet (each session keeps its own link
# alive); the fire is a Fleet batch calibration. Real-time radar is
# deliberately NOT streamed here (status/result only). For the live per-zone
# view use single-device `ms605 monitor` / interactive [4].
# ===========================================================================
_BATCH_STATUS_LABEL: dict[str, str] = {
    "connected": "미보정 (연결 유지된 채 종료)",
    "lost": "연결 끊김 (대기 중 유실 -- 버튼 재입력 필요)",
    "calibrated_ok": "보정 성공",
    "calibrated_fail": "보정 실패",
    "calibration_timeout": "보정 결과 타임아웃",
    "calibration_lost": "보정 중 연결 끊김",
    "calibration_error": "보정 오류",
}


@dataclass
class SummaryRow:
    """One sensor's line in the batch/clone summary: a _BATCH_STATUS_LABEL or
    _CLONE_STATUS_LABEL key and its detail."""

    name: str
    address: str
    status: str = "connected"
    detail: str = ""


def _name(session: DeviceSession) -> str:
    return session.name or "(이름없음)"


def _calibration_status(result: CalibrationResult | None) -> tuple[str, str]:
    """A core calibration result as the summary's (status, detail) -- the
    mapping table in docs/CORE_API.md 12.3."""
    if result is None:
        return "connected", ""
    state = result.state
    if state is CalibrationState.SUCCEEDED:
        return "calibrated_ok", result.detail
    if state is CalibrationState.TIMEOUT:
        return "calibration_timeout", ""
    if state is CalibrationState.LOST:
        if result.started:
            return "calibration_lost", result.error or result.detail
        return "lost", result.detail or result.error or ""
    if state is CalibrationState.FAILED:
        return ("calibrated_fail", "") if result.error is None else ("calibration_error", result.error)
    return "connected", ""  # CANCELLED: the CLI never cancels a batch


def resolve_target_datetime(at: str, now: datetime) -> datetime:
    """Resolve `--schedule HH:MM` to a concrete datetime relative to `now`.

    If that time of day has already passed today (or is exactly now), rolls
    over to tomorrow -- the intended use is "set up in the evening, fire after
    everyone has left, possibly past midnight"."""
    try:
        hh_str, mm_str = at.strip().split(":", 1)
        hh, mm = int(hh_str), int(mm_str)
    except ValueError as exc:
        raise ValueError(f"--schedule 형식이 잘못됨: {at!r} (HH:MM 24시간제)") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"--schedule 시각 범위 오류: {at!r} (00:00~23:59)")
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def format_batch_summary(devices: Sequence[SummaryRow]) -> str:
    lines = ["\n" + "=" * 60, " 최종 결과", "=" * 60]
    if not devices:
        lines.append("연결된 센서가 없었습니다.")
    for md in devices:
        label = _BATCH_STATUS_LABEL.get(md.status, md.status)
        detail = f" ({md.detail})" if md.detail else ""
        lines.append(f"  - {md.name:<24} {md.address}  -> {label}{detail}")
    ok_n = sum(1 for md in devices if md.status == "calibrated_ok")
    lines.append(f"\n{ok_n}/{len(devices)}대 보정 성공.")
    return "\n".join(lines)


def _fmt_hms(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _batch_event_log(fleet: Fleet, log: Callable[[str], None]) -> Callable[[Event], None]:
    """A bus subscriber turning the fleet's events into the batch log lines:
    link loss and keep-alive misses while waiting, the fire banner, and each
    sensor's start and result."""
    fired = False

    def who(ev: Event) -> str:
        session = fleet.sessions.get(ev.device_id) if ev.device_id else None
        return f"{_name(session) if session else '(이름없음)'} {ev.address}"

    def on_event(ev: Event) -> None:
        nonlocal fired
        ts = time.strftime("%H:%M:%S")
        if isinstance(ev, KeepAliveMissed):  # the device answered or the ACK was lost: the link is up
            if ev.kind == "error":
                what = "keep-alive 응답 오류 (연결 유지)"
            else:
                what = "keep-alive 응답 없음 (연결 유지, 재시도)"
            log(f"\n[{ts}] ⚠️  {who(ev)} {what}: {ev.error}")
        elif isinstance(ev, LinkStateChanged) and ev.state is LinkState.LOST and not fired:
            log(f"\n[{ts}] ⚠️  {who(ev)} 연결 끊김 (발사 전 대기 중): {ev.reason}")
        elif isinstance(ev, BatchChanged) and ev.state is BatchState.RUNNING:
            fired = True
            live = sum(
                1 for i in ev.device_ids if i in fleet.sessions and fleet.sessions[i].state is LinkState.CONNECTED
            )
            log("\n" + "=" * 60)
            log(f" 일괄 자동 보정 시작 -- {live}/{len(ev.device_ids)}대 (공간에 사람이 없어야 함)")
            log("=" * 60)
            if not live:
                log("연결 유지된 센서가 없어 보정을 건너뜁니다.")
        elif isinstance(ev, CalibrationStateChanged) and ev.state is CalibrationState.STARTING:
            log(f"\n[{ts}] 🔄 {who(ev)} 자동 보정 시작...")
        elif isinstance(ev, CalibrationResult):
            status, detail = _calibration_status(ev)
            line = {
                "calibrated_ok": "✅ {who} 보정 성공",
                "calibrated_fail": "❌ {who} 보정 실패",
                "calibration_timeout": "⏱️  {who} 보정 결과 타임아웃",
                "calibration_lost": "🔌 {who} 보정 중 연결 끊김: {detail}",
                "calibration_error": "❌ {who} 보정 오류: {detail}",
            }.get(status)
            if line is not None:  # "lost" before the fire was logged when it happened
                log(f"[{ts}] " + line.format(who=who(ev), detail=detail))

    return on_event


_GATHER_SELECT_PROMPT = "보정할 센서를 선택하세요"


async def _batch_gather_menu(
    fleet: Fleet,
    scan_secs: float,
    log: Callable[[str], None],
    *,
    select_prompt: str = _GATHER_SELECT_PROMPT,
    exclude: frozenset[str] = frozenset(),
) -> None:
    """Default gather: scan once, show a checkbox of found devices, let the
    operator tick several (Space toggles, Enter confirms), then connect each
    into `fleet`. Rescans when nothing is picked / no devices found.

    `select_prompt` overrides the checkbox message (the clone flow reuses this
    for target selection). `exclude` drops advertised devices by address (e.g.
    the clone source). Each sensor's session keeps its link alive from the
    moment it connects, so early ones don't idle-drop while later ones connect."""
    while True:
        with _ui.status(f"MS605 검색 중... (스캔 {scan_secs:.0f}s)"):
            try:
                devices = await fleet.scan()
            except Exception as exc:  # noqa: BLE001 - surface scan failure, keep offering retry
                _ui.error(f"스캔 오류: {exc}")
                devices = []
        if exclude:
            devices = [d for d in devices if d.address not in exclude]
        if not devices:
            _ui.warn("MS605가 안 보입니다. 각 센서 버튼을 눌러 광고 모드로 전환하세요.")
            _ui.muted("  (Meross 앱은 종료 — BLE는 한 번에 하나만 연결)")
            if not await _ui.confirm("다시 검색할까요?", default=True):
                return
            continue

        options = [(_fmt_device_choice(dev), dev) for dev in devices]
        selected = await _ui.checkbox(select_prompt, options, checked=[])
        if not selected:
            if not await _ui.confirm("선택된 센서가 없습니다. 다시 검색할까요?", default=True):
                return
            continue

        for dev in selected:
            name = dev.name or "(이름없음)"
            log(f"\n[{time.strftime('%H:%M:%S')}] 🔗 연결 시도: {name} {dev.address} ...")
            try:
                await fleet.connect(dev)
                log(f"[{time.strftime('%H:%M:%S')}] ✅ 연결됨 (누적 {len(fleet.sessions)}대): {name} {dev.address}")
            except Exception as exc:  # noqa: BLE001 - one failure shouldn't abort the batch
                log(f"[{time.strftime('%H:%M:%S')}]    ✗ {name} {dev.address} 연결 실패: "
                    f"{friendly_ble_error(exc, dev.address)}")
        if fleet.sessions:
            return
        log("\n선택한 센서를 하나도 연결하지 못했습니다. 버튼을 다시 누른 뒤 재검색하세요.")


async def _batch_gather_collect(fleet: Fleet, log: Callable[[str], None]) -> None:
    """`--collect` gather: the fleet keeps scanning and connects every sensor
    the instant it advertises (press each sensor's button in turn); Enter
    finishes. This is the former ms605-schedule setup phase -- robust to the
    MS605's brief post-button connectable window because it grabs each sensor
    immediately. Enter lets in-flight connect attempts finish; however it ends
    (Enter, Ctrl-C, cancel), nothing is still scanning or connecting on return."""
    names: dict[str, str] = {}

    def accept(dev) -> bool:
        names[dev.address] = dev.name or "(이름없음)"  # for the attempt line below
        return True

    def on_event(ev: Event) -> None:
        ts = time.strftime("%H:%M:%S")
        if isinstance(ev, LinkStateChanged) and ev.state is LinkState.CONNECTING:
            log(f"\n[{ts}] 🔗 새 기기 발견, 연결 시도: {names.get(ev.address, '(이름없음)')} {ev.address}")
        elif isinstance(ev, SensorGathered):
            log(f"[{ts}] ✅ 연결됨 (누적 {len(fleet.sessions)}대): {ev.name or '(이름없음)'} {ev.address}")
        elif isinstance(ev, GatherFailed):
            log(f"[{ts}]    ✗ {ev.name or '(이름없음)'} {ev.address} 연결 실패 "
                f"(버튼을 방금 눌렀는지 확인): {friendly_ble_error(MS605Error(ev.error), ev.address)}")

    unsubscribe = fleet.bus.subscribe(on_event)
    fleet.start_gather(accept=accept)
    try:
        log("\n" + "=" * 60)
        log(" 수집 단계 -- 각 센서 버튼을 순서대로 눌러 연결하세요 (--collect)")
        log("=" * 60)
        log("주의: BLE는 한 번에 하나만 연결됩니다. 각 센서에서 Meross 앱이 연결 중이면")
        log("      먼저 종료/백그라운드 처리하세요.")
        log("버튼을 누른 센서가 스캔에 잡히는 대로 자동으로 연결합니다.")
        log("모든 센서를 연결했으면 Enter 를 눌러 다음 단계로 진행하세요.\n")
        await ainput("연결 완료 후 Enter 입력: ")
        if fleet.connecting:  # a sensor pressed just before Enter still joins the batch
            log(f"   (진행 중인 연결 시도 {fleet.connecting}건 마무리 대기...)")
        await fleet.stop_gather(finish_pending=True)
    finally:
        # nothing may keep scanning or connecting once we return, or a late
        # link would outlive the caller's release.
        await fleet.stop_gather()
        unsubscribe()


async def _batch_gather_address(
    fleet: Fleet, scan_secs: float, prefer_address: str, log: Callable[[str], None]
) -> None:
    """`--address` gather: wait for one specific device to advertise, then
    connect just it (a batch of one). Rescans until it appears or the operator
    cancels; retries the button-press loop on a failed connect."""
    want = prefer_address.strip().lower()
    while True:
        log(f"\n🔍 지정 기기 검색 중... ({prefer_address}, 스캔 {scan_secs:.0f}s)")
        try:
            devices = await fleet.scan()
        except Exception as exc:  # noqa: BLE001
            log(f"  (스캔 오류: {exc})")
            devices = []
        match = next(
            (d for d in devices if d.address.lower() == want or (d.name or "").lower() == want),
            None,
        )
        if match is None:
            _ui.warn(f"지정 기기({prefer_address})가 안 보입니다. 버튼을 눌러 광고 모드로 전환 후 대기.")
            if not await _ui.confirm("다시 검색할까요?", default=True):
                return
            continue
        name = match.name or "(이름없음)"
        try:
            await fleet.connect(match)
            log(f"✅ 연결됨: {name} {match.address}")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"   ✗ 연결 실패: {friendly_ble_error(exc, match.address)} — 버튼 누르고 재시도.")
            await asyncio.sleep(2.0)


async def _hold_heartbeat(
    fleet: Fleet, device_ids: Sequence[str], target: datetime, interval: float, log: Callable[[str], None]
) -> None:
    """`--schedule` wait: a status line every max(interval, 60) s until `target`."""
    beat = max(interval, 60.0)
    while datetime.now() < target:
        await asyncio.sleep(min(beat, max(1.0, (target - datetime.now()).total_seconds())))
        if datetime.now() >= target:
            return
        alive = sum(
            1 for i in device_ids if i in fleet.sessions and fleet.sessions[i].state is LinkState.CONNECTED
        )
        left = target - datetime.now()
        log(f"  ... [{time.strftime('%H:%M:%S')}] 대기 중: {alive}/{len(device_ids)}대 "
            f"연결 유지, 발사까지 {_fmt_hms(left)}")


async def _batch_release(fleet: Fleet, log: Callable[[str], None]) -> list[DeviceSession]:
    """Always disconnect everything (each bounded, so a wedged peer can't hang
    us). Returns the sessions that were held, in gather order, for the summary."""
    sessions = list(fleet.sessions.values())
    if sessions:
        log("\n" + "=" * 60)
        log(" 연결 해제")
        log("=" * 60)
    await fleet.aclose()
    return sessions


def _new_fleet(scan_secs: float, connect_timeout: float, keepalive_interval: float) -> Fleet:
    storage = Storage()
    return Fleet(
        Registry(storage),
        storage,
        scan_secs=scan_secs,
        connect_timeout=connect_timeout,
        keepalive_interval=keepalive_interval,
    )


async def run_batch_calibration(
    *,
    scan_secs: float,
    connect_timeout: float,
    collect: bool = False,
    schedule: str | None = None,
    keepalive_interval: float = 15.0,
    calibration_timeout: float = 200.0,
    log_file: str | None = None,
    prefer_address: str | None = None,
) -> int:
    """`calibrate` subcommand entry: connect several sensors and auto-calibrate
    them together (absorbs the former `ms605-schedule`). See the batch section
    header above for the gather (--collect/--address) x fire (--schedule) matrix.
    Returns 0 only if every connected sensor calibrated OK."""
    fh = open(log_file, "a", encoding="utf-8") if log_file else None

    def log(msg: str = "") -> None:
        print(msg)
        if fh is not None:
            print(msg, file=fh)
            fh.flush()

    try:
        log("=" * 60)
        log(" Meross MS605 다중 센서 일괄 자동 보정")
        log("=" * 60)
        log("주의: BLE는 한 번에 하나만 연결됩니다. 각 센서의 Meross 앱을 먼저 종료하세요.\n")
        log(adapter_status_summary())

        target: datetime | None = None
        if schedule:
            try:
                target = resolve_target_datetime(schedule, datetime.now())
            except ValueError as exc:
                log(f"오류: {exc}")
                return 2
            log(f"\n예약 발사 시각: {target.strftime('%Y-%m-%d %H:%M')} "
                f"(지금: {datetime.now().strftime('%Y-%m-%d %H:%M')})")

        try:
            fleet = _new_fleet(scan_secs, connect_timeout, keepalive_interval)
        except StorageError as exc:
            log(f"레지스트리 파일 오류: {exc}")
            return 2
        fleet.bus.subscribe(_batch_event_log(fleet, log))
        lost: dict[str, str] = {}  # device id -> why its link dropped

        def note_loss(ev: Event) -> None:
            if isinstance(ev, LinkStateChanged) and ev.state is LinkState.LOST and ev.device_id:
                lost[ev.device_id] = ev.reason

        fleet.bus.subscribe(note_loss)
        # filled as each sensor finishes, so an interrupted fire still reports the ones already done
        results: dict[str, CalibrationResult] = {}

        def note_result(ev: Event) -> None:
            if isinstance(ev, CalibrationResult) and ev.device_id:
                results[ev.device_id] = ev

        fleet.bus.subscribe(note_result)
        rows: list[SummaryRow] = []

        def summary_row(session: DeviceSession) -> SummaryRow:
            result = results.get(session.device_id)
            if result is None and session.state is LinkState.LOST:  # dropped before any fire
                return SummaryRow(_name(session), session.address, "lost", lost.get(session.device_id, ""))
            return SummaryRow(_name(session), session.address, *_calibration_status(result))
        try:
            if prefer_address:
                await _batch_gather_address(fleet, scan_secs, prefer_address, log)
            elif collect:
                await _batch_gather_collect(fleet, log)
            else:
                await _batch_gather_menu(fleet, scan_secs, log)

            sessions = list(fleet.sessions.values())
            if not sessions:
                log("\n연결된 센서가 없어 종료합니다.")
                return 1
            log(f"\n연결 완료 -- 총 {len(sessions)}대:")
            for session in sessions:
                log(f"   - {_name(session)} {session.address}")
            ids = list(fleet.sessions)

            if target is not None:
                log("\n" + "=" * 60)
                log(f" 대기 단계 -- {target.strftime('%Y-%m-%d %H:%M')} 까지 {len(ids)}대 연결 유지")
                log("=" * 60)
                try:
                    batch = fleet.calibrate(ids, start=target, timeout=calibration_timeout)
                except ValueError:  # the target passed while gathering: fire at once
                    batch = fleet.calibrate(ids, timeout=calibration_timeout)
                hb = asyncio.create_task(_hold_heartbeat(fleet, ids, target, keepalive_interval, log))
                try:
                    await batch.wait()
                finally:
                    hb.cancel()
                    await asyncio.gather(hb, return_exceptions=True)
            else:
                log("\n" + "-" * 60)
                log("보정은 감지 공간에 사람이 없는 상태에서 시작해야 합니다.")
                await ainput("공간을 비운 뒤 준비되면 Enter를 눌러 전체 센서 일괄 보정을 시작하세요: ")
                await fleet.calibrate(ids, timeout=calibration_timeout).wait()
        finally:
            # whatever ended the run (incl. Ctrl-C mid-gather), every link is
            # released and the operator still sees every sensor's outcome
            rows = [summary_row(s) for s in fleet.sessions.values()]  # before release resets the states
            await _batch_release(fleet, log)
            if rows:
                log(format_batch_summary(rows))
        return 0 if rows and all(row.status == "calibrated_ok" for row in rows) else 1
    finally:
        if fh is not None:
            fh.close()


# ===========================================================================
# CONFIG CLONE  (the `clone` subcommand)
#
# Capture one sensor's writable settings into a ConfigProfile, then apply the
# selected sections onto one or more target sensors -- and/or save/load the
# profile as a JSON file. See ms605.models.ConfigProfile / PROFILE_SECTIONS for
# the cloneable surface; the writes are a Fleet draft (snapshot, write, polled
# verify) -- see ms605.fleet.
# ===========================================================================
_CLONE_STATUS_LABEL: dict[str, str] = {
    "connected": "미적용 (연결만 됨)",
    "clone_ok": "적용 성공 (반영 확인됨)",
    "clone_partial": "일부 항목 미반영",
    "clone_unverified": "적용됨 (반영 확인 실패)",
    "clone_error": "적용 오류",
}


def _fmt_sensitivity(value: int) -> str:
    try:
        return f"{value} ({Sensitivity(value).name})"
    except ValueError:
        return str(value)


def _fmt_detect_mode(value: int) -> str:
    try:
        return f"{value} ({DetectMode(value).name})"
    except ValueError:
        return str(value)


def _fmt_zone_thresholds_block(pairs: Sequence[tuple[int, int]]) -> list[str]:
    out = ["  존 임계값(tag51)      :", "     #  거리      Trigger   Maintain"]
    for i, (t, m) in enumerate(pairs):
        dist = f"{FALLBACK_DISTANCES_M[i]:.1f} m" if i < len(FALLBACK_DISTANCES_M) else "   ?  "
        out.append(f"     {i}  {dist:>7}   {t:>6}   {m:>6}")
    return out


def format_profile_table(profile: ConfigProfile) -> str:
    """Human-readable dump of a ConfigProfile's populated sections."""
    render: dict[str, Callable[[object], list[str]]] = {
        "sensitivity": lambda v: [f"  민감도(tag61)         : {_fmt_sensitivity(v)}"],
        "detect_mode": lambda v: [f"  감지모드(tag52)       : {_fmt_detect_mode(v)}"],
        "zone_enable": lambda v: [
            f"  존 활성화(tag50)      : {''.join('1' if b else '0' for b in v)}"
        ],
        "zone_thresholds": _fmt_zone_thresholds_block,
        "subsensor_zones": lambda v: [
            "  센서별 구역(tag48)    : " + "  ".join(f"S{i + 1}={list(z)}" for i, z in enumerate(v))
        ],
        "subsensor_timing": lambda v: [
            "  센서별 타이밍(tag49)  : "
            + "  ".join(f"S{i + 1}={p}/{a}s" for i, (p, a) in enumerate(v))
        ],
        "subsensor_enable": lambda v: [
            "  서브센서 enable(tag41): "
            + " ".join(f"S{i + 1}={'on' if b else 'off'}" for i, b in enumerate(v))
        ],
    }
    lines: list[str] = []
    if profile.source_name or profile.source_address:
        lines.append(f"  (원본: {profile.source_name or '?'}  {profile.source_address or ''})".rstrip())
    present = profile.sections_present()
    if not present:
        lines.append("  (비어 있음)")
    for key in present:
        lines += render[key](getattr(profile, key))
    return "\n".join(lines)


def save_profile(profile: ConfigProfile, path: Path) -> Path:
    """Write `profile` to `path` as pretty-printed JSON (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def load_profile(path: Path) -> ConfigProfile:
    """Load a ConfigProfile from a JSON file written by save_profile()."""
    with path.open("r", encoding="utf-8") as f:
        return ConfigProfile.from_dict(json.load(f))


def _parse_section_keys(text: str) -> list[str]:
    """Split a comma/space-separated --only/--skip list into section tokens."""
    return [tok for tok in text.replace(",", " ").split() if tok]


def resolve_sections(
    profile: ConfigProfile, *, only: str | None = None, skip: str | None = None
) -> list[str]:
    """Turn --only/--skip CLI text into a concrete, registry-ordered list of
    sections to apply, restricted to those actually present in `profile`.
    Raises ValueError on an unknown key or if both --only and --skip are given."""
    if only and skip:
        raise ValueError("--only 와 --skip 은 함께 쓸 수 없습니다.")
    present = list(profile.sections_present())
    chosen = only or skip
    if chosen is None:
        return present
    keys = _parse_section_keys(chosen.lower())
    unknown = [k for k in keys if k not in PROFILE_SECTION_KEYS]
    if unknown:
        raise ValueError(f"알 수 없는 항목: {unknown} (가능: {list(PROFILE_SECTION_KEYS)})")
    if only:
        return [k for k in present if k in keys]
    return [k for k in present if k not in keys]


async def _select_sections_interactive(profile: ConfigProfile) -> list[str] | None:
    """Checkbox multi-select of which present sections to apply (all pre-checked
    -- Enter copies everything). Returns the chosen keys (possibly empty), or
    None if the operator cancels the headless fallback."""
    present = list(profile.sections_present())
    if not present:
        return []
    options = [(PROFILE_SECTION_LABELS[key], key) for key in present]
    return await _ui.checkbox("복제할 설정 항목을 선택하세요 (기본: 전체)", options, checked=present)


def _clone_status(result: ApplyResult | None) -> tuple[str, str]:
    """A core apply result as the summary's (status, detail) -- docs/CORE_API.md 12.3."""
    if result is None:
        return "connected", ""
    if result.status is ApplyStatus.FAILED:
        return "clone_error", result.error or ""
    if result.status is ApplyStatus.UNVERIFIED:
        return "clone_unverified", f"반영 확인 실패: {result.error}"
    notes: list[str] = []
    if result.skipped:
        notes.append("건너뜀: " + ", ".join(result.skipped))
    if result.mismatched:
        notes.append("미반영: " + ", ".join(result.mismatched))
    return ("clone_partial" if result.mismatched else "clone_ok"), " / ".join(notes)


async def _clone_apply_all(
    fleet: Fleet,
    profile: ConfigProfile,
    sections: Sequence[str],
    log: Callable[[str], None],
) -> dict[str, ApplyResult]:
    """Apply the profile's `sections` to every gathered target in turn (an
    absolute Fleet draft: snapshot, write, polled verify), logging each target
    as it starts and ends. One target failing -- however unexpectedly -- never
    stops the rest; targets queued behind others stay connected through their
    sessions' keep-alive."""

    announced: set[str] = set()

    def on_event(ev: Event) -> None:
        session = fleet.sessions.get(ev.device_id) if isinstance(ev, (BusyChanged, ApplyResult)) else None
        if session is None:
            return
        who = f"{_name(session)} {session.address}"
        ts = time.strftime("%H:%M:%S")
        # a target that is not connected never takes the lock: announce it with its result
        if (isinstance(ev, BusyChanged) and ev.busy == "apply") or (
            isinstance(ev, ApplyResult) and ev.device_id not in announced
        ):
            announced.add(ev.device_id)
            log(f"\n[{ts}] 🔧 {who} 설정 적용 중...")
        if isinstance(ev, ApplyResult):
            status, detail = _clone_status(ev)
            if status == "clone_error":
                log(f"[{ts}] ❌ {who} 적용 오류: {detail}")
            elif status == "clone_unverified":
                log(f"[{ts}] ⚠️  {who} 적용됨(반영 확인 실패): {ev.error}")
            elif status == "clone_partial":
                log(f"[{ts}] ⚠️  {who} 일부 미반영: {list(ev.mismatched)}")
            else:
                log(f"[{ts}] ✅ {who} 적용 완료 ({len(ev.applied)}개 항목 반영 확인)")

    unsubscribe = fleet.bus.subscribe(on_event)
    try:
        return await fleet.apply(Draft(list(fleet.sessions), bulk=SensorChanges.from_profile(profile, sections)))
    finally:
        unsubscribe()


def format_clone_summary(devices: Sequence[SummaryRow], sections: Sequence[str]) -> str:
    """Body of the clone result (rendered inside a titled panel by run_clone)."""
    lines = ["적용 항목: " + (", ".join(sections) if sections else "(없음)")]
    if not devices:
        lines.append("대상 센서가 없었습니다.")
    for md in devices:
        label = _CLONE_STATUS_LABEL.get(md.status, md.status)
        detail = f" ({md.detail})" if md.detail else ""
        lines.append(f"  - {md.name:<24} {md.address}  -> {label}{detail}")
    ok_n = sum(1 for md in devices if md.status == "clone_ok")
    lines.append(f"\n{ok_n}/{len(devices)}대 적용 성공.")
    return "\n".join(lines)


async def run_clone(
    *,
    scan_secs: float,
    connect_timeout: float,
    source_address: str | None = None,
    save_path: str | None = None,
    from_file: str | None = None,
    only: str | None = None,
    skip: str | None = None,
    no_apply: bool = False,
    assume_yes: bool = False,
    keepalive_interval: float = 15.0,
) -> int:
    """`clone` subcommand entry: capture one sensor's writable settings (or load
    them from a JSON profile), let the operator pick which sections to copy, and
    apply them onto one or more freshly-selected target sensors. Returns 0 only
    if every target applied cleanly (or on a successful --no-apply read/save)."""
    _ui.header("Meross MS605 설정 복제 (clone)", "한 센서의 설정을 읽어 다른 센서에 복제")
    _ui.muted("주의: BLE는 한 번에 하나만 연결됩니다. 각 센서의 Meross 앱을 먼저 종료하세요.")
    _ui.muted(adapter_status_summary())

    exclude_addr: str | None = None
    # -- 1. obtain the profile (from a file, or by reading a source device) --
    if from_file:
        if source_address or save_path:
            _ui.muted("(--from-file 사용 시 --source/--save 는 무시됩니다.)")
        try:
            profile = load_profile(Path(from_file))
        except (OSError, ValueError, FrameError, json.JSONDecodeError) as exc:
            _ui.error(f"프로파일을 불러올 수 없습니다: {exc}")
            return 2
        _ui.success(f"프로파일 불러옴: {from_file}")
    else:
        device = await discover_and_select(
            scan_secs, auto_select=True, prefer_address=source_address
        )
        source = await connect_with_retry(device, scan_secs, connect_timeout)
        try:
            with _ui.status("소스 디바이스 설정을 읽는 중..."):
                cfg = await _read_config(source)
        finally:
            _ui.muted("소스 연결을 종료합니다...")
            await source.close()
        profile = ConfigProfile.from_config(
            cfg, source_name=device.name, source_address=device.address
        )
        exclude_addr = device.address

    _ui.panel(format_profile_table(profile), title="복제할 설정 프로파일")

    if save_path and not from_file:
        try:
            saved = save_profile(profile, Path(save_path))
        except OSError as exc:
            _ui.warn(f"프로파일 저장 실패: {exc}")
        else:
            _ui.success(f"프로파일 저장됨: {saved}")

    # -- 2. read/save-only mode: nothing to apply, so stop right after the
    #    read/save (don't prompt for sections we're never going to write). --
    if no_apply:
        _ui.muted("--no-apply: 프로파일 읽기/저장만 하고 종료합니다.")
        return 0

    # -- 3. choose which sections to apply --
    if only or skip:
        try:
            sections = resolve_sections(profile, only=only, skip=skip)
        except ValueError as exc:
            _ui.error(str(exc))
            return 2
    else:
        sections = await _select_sections_interactive(profile)
        if sections is None:
            _ui.muted("취소되었습니다.")
            return 1
    if not sections:
        _ui.warn("적용할 항목이 없습니다. 종료합니다.")
        return 1
    _ui.info("적용할 항목: " + ", ".join(PROFILE_SECTION_LABELS[k] for k in sections))

    # Validate the chosen sections once, before any target is connected or
    # written -- they would be rejected on every target anyway, after the
    # operator already picked and connected them all.
    try:
        profile.validate(sections)
    except MS605Error as exc:
        _ui.error(f"프로파일이 올바르지 않아 적용할 수 없습니다: {exc}")
        return 2

    # -- 4. gather targets (excluding the source), apply, always release --
    exclude = frozenset({exclude_addr}) if exclude_addr else frozenset()
    try:
        fleet = _new_fleet(scan_secs, connect_timeout, keepalive_interval)
    except StorageError as exc:
        _ui.error(f"레지스트리 파일 오류: {exc}")
        return 2
    results: dict[str, ApplyResult] = {}
    try:
        await _batch_gather_menu(
            fleet,
            scan_secs,
            print,
            select_prompt="설정을 복제할 대상 센서를 선택하세요",
            exclude=exclude,
        )
        if not fleet.sessions:
            _ui.warn("대상 센서가 없어 종료합니다.")
            return 1
        _ui.info(f"대상 -- 총 {len(fleet.sessions)}대:")
        for session in fleet.sessions.values():
            _ui.muted(f"   - {_name(session)} {session.address}")
        if not assume_yes:
            if not await _ui.confirm("위 대상에 설정을 적용할까요?", default=False):
                _ui.muted("취소되었습니다.")
                return 1
        results = await _clone_apply_all(fleet, profile, sections, print)
    finally:
        targets = await _batch_release(fleet, print)

    rows = [SummaryRow(_name(s), s.address, *_clone_status(results.get(s.device_id))) for s in targets]
    ok_all = bool(rows) and all(row.status == "clone_ok" for row in rows)
    _ui.panel(
        format_clone_summary(rows, sections).strip(),
        title="클론 결과",
        style="ok" if ok_all else "warn",
    )
    return 0 if ok_all else 1


# ===========================================================================
# FLOW 2: DETAILED ADJUSTMENT (Trigger / Maintain per distance)
# ===========================================================================
async def _prompt_threshold_column(label: str, dists: Sequence[float], current: Sequence[int]) -> list[int | None]:
    _ui.rule(f"{label} — 거리별 값 (0~500, Enter=현재값 유지)")
    out: list[int | None] = []
    for i, dist in enumerate(dists):
        while True:
            raw = (await _ui.text(f"{dist:.1f} m (현재 {current[i]}):")).strip()
            if raw == "":
                out.append(None)
                break
            if raw.isdigit() and THRESHOLD_MIN <= int(raw) <= THRESHOLD_MAX:
                out.append(int(raw))
                break
            _ui.warn(f"0~{THRESHOLD_MAX} 범위의 정수를 입력하세요.")
    return out


async def flow_detailed_adjustment(session: DeviceSession) -> None:
    _ui.header("세부 조정", "거리별 Trigger / Maintain 임계값")
    cfg = await _read_config(session)
    dists = zone_distances(cfg)
    _ui.panel(format_config_table(cfg), title="현재 설정")

    mode = await _ui.select(
        "조정할 모드를 선택하세요",
        [
            ("Presence Trigger  (재실 감지 개시 임계값)", "1"),
            ("Presence Maintain (재실 유지 임계값)", "2"),
            ("둘 다", "3"),
            ("취소", "c"),
        ],
    )
    if mode not in ("1", "2", "3"):
        _ui.muted("취소되었습니다.")
        return

    cur_trig = [zt.trigger for zt in cfg.zone_thresholds]
    cur_maint = [zt.maintain for zt in cfg.zone_thresholds]
    new_trig: list[int | None] = [None] * len(cur_trig)
    new_maint: list[int | None] = [None] * len(cur_maint)

    if mode in ("1", "3"):
        new_trig = await _prompt_threshold_column("Presence Trigger", dists, cur_trig)
    if mode in ("2", "3"):
        new_maint = await _prompt_threshold_column("Presence Maintain", dists, cur_maint)

    pairs = merge_thresholds(cfg, new_trig, new_maint)
    print("\n적용할 값:")
    print("  #  거리      Trigger   Maintain")
    for i, (t, m) in enumerate(pairs):
        d = dists[i] if i < len(dists) else 0.0
        mark_t = "*" if new_trig[i] is not None else " "
        mark_m = "*" if new_maint[i] is not None else " "
        print(f"  {i}  {d:.1f} m   {t:>6}{mark_t}   {m:>6}{mark_m}   (* = 변경)")

    if not await _ui.confirm("이 값으로 디바이스에 적용할까요?", default=False):
        _ui.muted("취소되었습니다.")
        return

    # the user just spent time typing values; re-establish the link if it
    # dropped anyway before the write.
    await ensure(session)
    # the device ACKs the write immediately but shows it on read-back only
    # ~1 s later: the core polls the config until it reflects what we wrote.
    confirmed = await _apply(session, SensorChanges(zone_thresholds=_absolute_thresholds(pairs)))
    print("✅ 적용 요청 완료. 디바이스에 반영되기를 기다리는 중...")
    if confirmed:
        print("✅ 반영 확인됨.")
    else:
        print("⚠️  제한 시간 내 반영을 확인하지 못했습니다 (아래는 현재 읽은 값).")
    cfg2 = await _read_config(session)
    print(format_config_table(cfg2))
    print(f"\n민감도는 {cfg2.sensitivity}"
          + (" (수동 조정 시 CUSTOM/4로 전환됩니다)" if cfg2.sensitivity == 4 else "")
          + " 입니다.")


# ===========================================================================
# FLOW 3: LIVE MONITOR (PIR / radar / per-sensor presence / per-zone, real time)
# ===========================================================================
def _format_sensor_presence(snap) -> str:
    return " ".join(
        f"Sensor{i + 1}={'재실' if present else '부재'}"
        for i, present in enumerate(snap.sub_sensor_presence)
    )


def _format_zone_presence(snap) -> str:
    return "".join("1" if z.trigger_active else "0" for z in snap.zones)


_MONITOR_BAR_WIDTH = 16
_MONITOR_TICK = 5  # threshold pinned at this fixed column (~1/3 in) for every bar


def _monitor_value(cur: int, thr: int, hot: bool) -> Text:
    return Text(f"{cur:>3}/{thr:<3}", style="bold red" if hot else "grey70")


def render_monitor(state: dict, dists: Sequence[float]) -> Panel:
    """Build one frame of the live monitor: a header (timestamp / PIR /
    per-sub-sensor presence) above a per-zone table that shows *both* meters the
    device reports for each zone -- Presence Trigger (재실 트리거) and Presence
    Maintain (재실 유지) -- as current-vs-threshold bars. The threshold is pinned
    at a fixed column so it never drifts; the fill crosses it, in red, exactly
    when current > threshold. The trigger cur/thr text is red on the device's
    own trigger-active flag, which can disagree with that comparison. Pure --
    `state` is {'snap', 'pir', 'ts'}, updated by the event handler."""
    snap = state.get("snap")
    pir = state.get("pir")
    ts = state.get("ts", "--:--:--")

    head = Text()
    head.append(f"{ts}   ", style="grey58")
    head.append("PIR ", style="bold cyan")
    if pir is None:
        head.append("?  ", style="grey58")
    else:
        head.append("● 감지  " if pir else "○ 없음  ", style="bold red" if pir else "grey58")
    if snap is not None:
        for i, present in enumerate(snap.sub_sensor_presence):
            head.append(f" S{i + 1} ", style="bold cyan")
            head.append("재실" if present else "부재", style="bold green" if present else "grey58")

    if snap is None:
        body: object = Text("데이터 수신 대기 중...", style="grey58")
    else:
        table = Table(box=box.SIMPLE_HEAD, header_style="key", border_style="muted", padding=(0, 1))
        table.add_column("존", justify="right")
        table.add_column("재실 트리거", justify="left")
        table.add_column("cur/thr", justify="right")
        table.add_column("재실 유지", justify="left")
        table.add_column("cur/thr", justify="right")
        for z in snap.zones:
            dist = dists[z.index] if z.index < len(dists) else 0.0
            label = Text(f"Z{z.index} {dist:>4.1f}m", style="key")
            if not z.enabled:
                off = Text("off", style="grey30")
                table.add_row(label, off, "", off, "")
                continue
            table.add_row(
                label,
                _ui.meter(z.current_trigger, z.trigger_threshold, width=_MONITOR_BAR_WIDTH, tick_at=_MONITOR_TICK),
                _monitor_value(z.current_trigger, z.trigger_threshold, z.trigger_active),
                _ui.meter(z.current_maintain, z.maintain_threshold, width=_MONITOR_BAR_WIDTH, tick_at=_MONITOR_TICK),
                _monitor_value(
                    z.current_maintain, z.maintain_threshold, z.current_maintain > z.maintain_threshold
                ),
            )
        body = table

    footer = Text(
        "┃=임계값(고정)   █=현재값(┃를 넘으면 빨강)   ·   Enter를 눌러 종료\n"
        "트리거 cur/thr 빨강=기기 trigger-active 플래그",
        style="grey46",
    )
    return Panel(Group(head, Text(), body, footer),
                 title="[brand]실시간 감지값 모니터링[/]", border_style="accent", padding=(0, 2))


async def _flow_live_monitor_plain(session: DeviceSession) -> None:
    """Headless monitor: one line per tag55 push or PIR change (no Live region),
    for piped/logged runs where a full-screen bar graph can't render."""
    _ui.muted("기기가 보내는 값을 그대로 표시합니다. 종료하려면 Enter를 누르세요.\n")

    def on_event(ev: Event) -> None:
        ts = time.strftime("%H:%M:%S")
        if isinstance(ev, LiveRadar):  # an undecodable tag55 never gets here (FrameDropped)
            snap = ev.snapshot
            zones = " ".join(_format_radar_zone(z) for z in snap.zones)
            print(f"  [{ts}] {zones}")
            print(f"           {_format_sensor_presence(snap)}  |  구역별재실(0/1)={_format_zone_presence(snap)}")
        elif isinstance(ev, PirChanged):
            print(f"  [{ts}] PIR: {'감지' if ev.detected else '없음'}")

    unsubscribe = session.bus.subscribe(on_event)
    try:
        try:
            await session.acquire_live()
        except MS605Error as exc:
            print(f"(live 출력 활성화 실패 — 계속 진행: {exc})")
        await ainput()
    finally:
        unsubscribe()
        await session.release_live()


async def flow_live_monitor(session: DeviceSession) -> None:
    """Real-time view of PIR state, per-zone radar current/threshold values,
    per-sub-sensor presence (the device's own final call -- not host-computed),
    and per-zone trigger state, straight from tag55 pushes (PIR: tag56, which the
    session reads while live output is on). On a terminal this is a live bar
    graph (redrawn on each update); headless it degrades to one line per update.
    Enter stops it."""
    await ensure(session)
    _ui.header("실시간 감지값 모니터링", "PIR · 레이더 존별 · 센서별 재실 · 구역별 재실")
    if not _ui.interactive():
        await _flow_live_monitor_plain(session)
        return

    dists = zone_distances(await _read_config(session))
    state: dict = {"snap": None, "pir": None, "ts": time.strftime("%H:%M:%S")}
    with _ui.make_live(render_monitor(state, dists)) as live:

        def on_event(ev: Event) -> None:
            if isinstance(ev, LiveRadar):  # a truncated tag55 keeps the last good frame on screen
                state["snap"] = ev.snapshot
            elif isinstance(ev, PirChanged):
                state["pir"] = ev.detected
            else:
                return
            state["ts"] = time.strftime("%H:%M:%S")
            live.update(render_monitor(state, dists), refresh=True)

        unsubscribe = session.bus.subscribe(on_event)
        try:
            try:
                await session.acquire_live()
            except MS605Error:
                pass
            await ainput()  # Enter stops the monitor
        finally:
            unsubscribe()
            await session.release_live()


# ===========================================================================
# FLOW 4: ZONE & SUB-SENSOR CONFIG (enable, Sensor1/2/3 zone assignment, timing)
# ===========================================================================
async def flow_zone_enable(session: DeviceSession) -> None:
    """View and toggle which of the 7 zones are active (tag50 bitmask)."""
    await ensure(session)
    cfg = await _read_config(session)
    dists = zone_distances(cfg)
    enabled = list(cfg.zones_enabled())
    _ui.header("구역 활성화", "감지 range에 포함할 구역 선택")

    options = [
        (f"구역 {i}  ·  {dists[i]:.1f} m  (현재 {'켜짐' if enabled[i] else '꺼짐'})", i)
        for i in range(7)
    ]
    checked = [i for i in range(7) if enabled[i]]
    chosen = await _ui.checkbox("켜둘 구역을 선택하세요 (전체 해제 = 모두 끄기)", options, checked=checked)
    if chosen is None:
        _ui.muted("취소되었습니다.")
        return

    new_enabled = [i in set(chosen) for i in range(7)]
    _ui.info("적용할 값: " + "".join("1" if e else "0" for e in new_enabled))
    if not await _ui.confirm("이 값으로 적용할까요?", default=False):
        _ui.muted("취소되었습니다.")
        return

    await ensure(session)
    await _apply(session, SensorChanges(zone_enable=new_enabled))
    _ui.success("적용 요청 완료.")
    cfg2 = await _read_config(session)
    _ui.info("현재 구역 활성화: " + "".join("1" if b else "0" for b in cfg2.zones_enabled()))


async def flow_subsensor_zones(session: DeviceSession) -> None:
    """View and reassign which zones (0-6) feed each of Sensor1/2/3 (tag48)."""
    await ensure(session)
    cfg = await _read_config(session)
    _ui.header("센서별 구역 지정", "Sensor1 / Sensor2 / Sensor3")
    for zm in cfg.segment_map:
        _ui.muted(f"  Sensor{zm.index + 1}: 구역 {list(zm.zones)}")

    new_zones: list[list[int]] = []
    for zm in cfg.segment_map:
        raw = (await _ui.text(
            f"Sensor{zm.index + 1} 구역 (0-6, 공백구분, Enter=현재 {list(zm.zones)} 유지):"
        )).strip()
        if raw == "":
            new_zones.append(list(zm.zones))
            continue
        try:
            zones = sorted({int(x) for x in raw.split()})
        except ValueError:
            _ui.error("숫자만 입력하세요. 전체 취소되었습니다.")
            return
        if any(not (0 <= z <= 6) for z in zones):
            _ui.error("구역 번호는 0~6 범위여야 합니다. 전체 취소되었습니다.")
            return
        new_zones.append(zones)

    _ui.info("적용할 값: " + "  ".join(f"S{i + 1}={z}" for i, z in enumerate(new_zones)))
    if not await _ui.confirm("이 값으로 적용할까요?", default=False):
        _ui.muted("취소되었습니다.")
        return

    await ensure(session)
    await _apply(session, SensorChanges(subsensor_zones=new_zones))
    _ui.success("적용 요청 완료.")
    cfg2 = await _read_config(session)
    for zm in cfg2.segment_map:
        _ui.muted(f"  Sensor{zm.index + 1}: 구역 {list(zm.zones)}")


async def flow_subsensor_timing(session: DeviceSession) -> None:
    """View and set each sub-sensor's presence/absence duration (tag49)."""
    await ensure(session)
    cfg = await _read_config(session)
    _ui.header("센서별 Presence/Absence Duration 설정")
    for t in cfg.presence_absence_times:
        _ui.muted(f"  Sensor{t.index + 1}: presence={t.presence_seconds}s  absence={t.absence_seconds}s")

    new_timings: list[tuple[int, int]] = []
    for t in cfg.presence_absence_times:
        raw = (await _ui.text(
            f"Sensor{t.index + 1} presence,absence 초 "
            f"(Enter=현재 {t.presence_seconds},{t.absence_seconds} 유지):"
        )).strip()
        if raw == "":
            new_timings.append((t.presence_seconds, t.absence_seconds))
            continue
        parts = raw.split(",")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            _ui.error("'presence,absence' 형식의 숫자를 입력하세요. 전체 취소되었습니다.")
            return
        presence, absence = int(parts[0]), int(parts[1])
        if not (0 <= presence <= 0xFFFF and 0 <= absence <= 0xFFFF):
            _ui.error("0~65535 범위여야 합니다. 전체 취소되었습니다.")
            return
        new_timings.append((presence, absence))

    _ui.info(
        "적용할 값: "
        + "  ".join(f"S{i + 1}={p}/{a}s" for i, (p, a) in enumerate(new_timings))
    )
    if not await _ui.confirm("이 값으로 적용할까요?", default=False):
        _ui.muted("취소되었습니다.")
        return

    await ensure(session)
    await _apply(session, SensorChanges(subsensor_timing=new_timings))
    _ui.success("적용 요청 완료.")
    cfg2 = await _read_config(session)
    for t in cfg2.presence_absence_times:
        _ui.muted(f"  Sensor{t.index + 1}: presence={t.presence_seconds}s  absence={t.absence_seconds}s")


# ===========================================================================
# main menu
# ===========================================================================
_MENU_ACTIONS: tuple[tuple[str, str], ...] = (
    ("🎯  자동 보정 (실시간 진행 표시)", "1"),
    ("🎚️   세부 조정 (Trigger / Maintain)", "2"),
    ("📄  현재 설정 읽기", "3"),
    ("📡  실시간 감지값 모니터링 (PIR/레이더/센서별/구역별)", "4"),
    ("🗺️   구역 활성화 (감지 range 포함 여부)", "5"),
    ("🧩  센서별 구역 지정 (Sensor1/2/3)", "6"),
    ("⏱️   센서별 Presence/Absence Duration 설정", "7"),
    ("🚪  종료", "q"),
)


async def main_menu(session: DeviceSession) -> None:
    while True:
        _ui.header("MS605 제어 메뉴", f"{_name(session)}  ·  {session.address}")
        choice = await _ui.select("작업을 선택하세요", _MENU_ACTIONS)
        if choice in ("q", None):
            return
        try:
            # the link may have dropped while the menu waited for input;
            # reconnect before any device operation.
            await ensure(session)
            if choice == "1":
                await flow_auto_calibration(session)
            elif choice == "2":
                await flow_detailed_adjustment(session)
            elif choice == "3":
                cfg = await _read_config(session)
                _ui.panel(format_config_table(cfg), title="현재 설정")
            elif choice == "4":
                await flow_live_monitor(session)
            elif choice == "5":
                await flow_zone_enable(session)
            elif choice == "6":
                await flow_subsensor_zones(session)
            elif choice == "7":
                await flow_subsensor_timing(session)
        except MS605Error as exc:
            # one failed action (timeout, dropped link, ...) must not end the session
            _ui.error(f"작업 실패: {exc}")
            _ui.muted("메뉴로 돌아갑니다. (연결이 끊겼다면 다음 작업 전에 자동으로 재연결합니다)")


# ===========================================================================
# flow runners — shared connect + teardown scaffold for interactive AND the
# one-shot subcommands (calibrate / read / set-zone / set-sensitivity / ...)
# ===========================================================================
async def _run_flow(
    scan_secs: float,
    connect_timeout: float,
    flow: Callable[[DeviceSession], Awaitable[None]],
    *,
    show_header: bool = True,
    auto_select: bool = False,
    prefer_address: str | None = None,
) -> int:
    """Scan → connect (with the button-press retry loop) → run one `flow`
    coroutine on the DeviceSession → always tear the link down within a bounded time.

    This is the single entry every mode funnels through: the interactive menu is
    just `flow=main_menu`, and each one-shot subcommand passes its own flow (with
    `auto_select=True` so device discovery needs no interactive input).
    `prefer_address` targets a specific device (see discover_and_select). The
    session's keep-alive holds the link through idle waits (menu / monitor)."""
    _ui.header("Meross MS605 BLE 제어 앱", "재실 감지 센서 · BLE 설정 채널")
    _ui.muted("주의: BLE는 한 번에 하나만 연결됩니다. Meross 앱이 연결 중이면 먼저 종료/백그라운드 처리하세요.")
    _ui.muted(adapter_status_summary())

    device = await discover_and_select(
        scan_secs, auto_select=auto_select, prefer_address=prefer_address
    )
    session = await connect_with_retry(device, scan_secs, connect_timeout)
    try:
        if show_header:
            await print_device_header(session)
        await flow(session)
    finally:
        _ui.muted("연결을 종료합니다...")
        # bounded: CoreBluetooth's disconnect can wedge if the peer already
        # vanished — the session never lets cleanup hang the CLI.
        await session.close()
    _ui.success("종료되었습니다.")
    return 0


async def run_interactive(
    scan_secs: float, connect_timeout: float, *, prefer_address: str | None = None
) -> int:
    return await _run_flow(
        scan_secs, connect_timeout, main_menu, show_header=True, prefer_address=prefer_address
    )


async def flow_read_config(session: DeviceSession) -> None:
    """One-shot 'read': the device header (printed by _run_flow with
    show_header=True) already shows the full decoded config, so this is a
    no-op placeholder that keeps the flow signature uniform."""
    return None


async def flow_set_zone(session: DeviceSession, pairs: Sequence[tuple[int, int]]) -> None:
    """One-shot 'set-zone': write all 7 (trigger, maintain) thresholds, then
    poll-confirm the device actually committed them (tag51 apply-lag)."""
    await ensure(session)
    cfg = await _read_config(session)
    print("\n현재 설정:")
    print(format_config_table(cfg))
    dists = zone_distances(cfg)
    print("\n적용할 값:")
    print("  #  거리      Trigger   Maintain")
    for i, (t, m) in enumerate(pairs):
        d = dists[i] if i < len(dists) else 0.0
        print(f"  {i}  {d:.1f} m   {t:>6}   {m:>6}")
    await ensure(session)
    confirmed = await _apply(session, SensorChanges(zone_thresholds=_absolute_thresholds(pairs)))
    print("\n✅ 적용 요청 완료. 디바이스에 반영되기를 기다리는 중...")
    if confirmed:
        print("✅ 반영 확인됨.")
    else:
        print("⚠️  제한 시간 내 반영을 확인하지 못했습니다 (아래는 현재 읽은 값).")
    print(format_config_table(await _read_config(session)))


async def flow_set_sensitivity(session: DeviceSession, level: int) -> None:
    """One-shot 'set-sensitivity': write tag61 (1 LOW / 2 MED / 3 HIGH / 4 CUSTOM)."""
    await ensure(session)
    await _apply(session, SensorChanges(sensitivity=level))
    print(f"\n✅ 민감도(tag61) {level} 적용 요청 완료.")
    cfg = await _read_config(session)
    try:
        name = Sensitivity(cfg.sensitivity).name
    except ValueError:
        name = "?"
    print(f"현재 민감도: {cfg.sensitivity} ({name})")


async def flow_read_dnd(session: DeviceSession) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        print(f"\nDND(방해금지) 상태: {await ms.read_dnd()}")


async def flow_set_dnd(session: DeviceSession, enabled: bool) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        await ms.set_dnd(enabled)
    print(f"\n✅ DND(방해금지) {enabled} 적용 요청 완료.")


async def flow_read_pir(session: DeviceSession) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        print(f"\nPIR 상태: {await ms.read_pir_state()}")


async def flow_read_subsensor_status(session: DeviceSession) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        statuses = await ms.read_sub_sensor_status()
    for status in statuses:
        print(
            f"  sub-sensor {status.index}: presence={status.has_presence} "
            f"presence_ts={status.presence_timestamp} absence_ts={status.absence_timestamp}"
        )


async def flow_sync_time(session: DeviceSession) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        await ms.set_time()
    print("\n✅ 디바이스 시계를 호스트의 UTC 시각으로 동기화했습니다.")


async def flow_read_history(session: DeviceSession, kind: str, detail: bool) -> None:
    await ensure(session)
    async with session.operation("read") as ms:
        if kind == "presence":
            records = await ms.read_presence_history(detail=detail)
        else:
            records = await ms.read_light_history()
    if not records:
        print("\n(기록 없음)")
        return
    print(f"\n{len(records)}개 기록:")
    for rec in records:
        print(f"  {rec}")


# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ms605",
        description=(
            "Meross MS605 재실 감지 센서 BLE 제어 앱 (대화형 + 원샷 서브커맨드).\n"
            "인자 없이 실행하면 대화형 메뉴, 서브커맨드를 주면 연결 후 그 동작만 수행하고 종료합니다.\n"
            "모든 모드는 '버튼-누름 재연결' 루프를 공유합니다 — MS605는 물리 버튼을 누른 "
            "직후 잠깐만, 그리고 한 번에 한 곳만 BLE 연결을 받습니다(Meross 앱을 먼저 종료)."
        ),
        epilog=(
            "서브커맨드\n"
            "  (없음)/interactive   대화형 메뉴(단일 기기): 자동보정 / 세부조정 / 설정읽기\n"
            "  calibrate|auto       여러 센서를 동시 연결해 일괄 자동 보정 (--collect/--schedule)\n"
            "  clone                한 센서의 설정을 읽어 다른 센서(들)에 복제 (--save/--from-file/--only/--skip)\n"
            "  read                 현재 설정(민감도/모드/존/임계값 등)을 출력\n"
            "  set-zone P0..P6      7개 존의 'trigger,maintain' 임계값을 쓰고 반영 확인\n"
            "  set-sensitivity N    민감도 1=LOW 2=MED 3=HIGH 4=CUSTOM\n"
            "  read-dnd / set-dnd N 방해금지(DND) 상태 읽기/쓰기 (N: 0|1)\n"
            "  read-pir             PIR 상태 읽기\n"
            "  read-subsensor-status  서브센서별 재실/부재 타임스탬프 읽기\n"
            "  sync-time            디바이스 시계를 호스트 UTC 시각으로 동기화\n"
            "  read-history KIND    이력 읽기 (KIND: presence|light, 미검증 기능)\n"
            "  monitor              실시간 감지값 모니터링 (PIR/레이더/센서별/구역별)\n"
            "  set-zone-enable      구역 활성화(감지 range 포함 여부) 대화형 설정\n"
            "  set-subsensor-zones  센서별(Sensor1/2/3) 구역 지정 대화형 설정\n"
            "  set-subsensor-timing 센서별 Presence/Absence Duration 대화형 설정\n"
            "\n"
            "전역 옵션(서브커맨드 앞/뒤 어디에 둬도 동작)\n"
            "  --address/--device   특정 기기만 대상(주소/UUID 또는 이름). 여러 대가 잡혀도 즉시 선택\n"
            "  --scan-secs N        스캔 시간(초, 기본 6)\n"
            "  --connect-timeout N  연결 타임아웃(초, 기본 12)\n"
            "\n"
            "예시\n"
            "  ms605                                  # 대화형(단일 기기)\n"
            "  ms605 calibrate                        # 스캔→다중/전체 선택→즉시 일괄 보정\n"
            "  ms605 calibrate --collect              # 버튼 누르는 대로 수집→일괄 보정\n"
            "  ms605 calibrate --schedule 02:00       # 새벽 2시에 무인 일괄 보정\n"
            "  ms605 clone                            # 소스 선택→설정 읽기→대상 다중 선택→복제\n"
            "  ms605 clone --source ms605-A --save private-profiles/a.json --no-apply\n"
            "  ms605 clone --from-file private-profiles/a.json --only zone_thresholds\n"
            "  ms605 --address <DEVICE-UUID> calibrate  # 특정 1대만\n"
            "  ms605 read\n"
            "  ms605 set-zone 95,40 85,40 75,40 60,40 55,40 40,35 35,28\n"
            "  ms605 set-sensitivity 3                # 민감도 HIGH\n"
            "  ms605 read-history presence --detail\n"
            "  ms605 monitor                           # 실시간 감지값 모니터링\n"
            "  ms605 --scan-secs 8 --address ms605 read"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_target_args(ap)

    sub = ap.add_subparsers(dest="command", metavar="<command>")
    p_interactive = sub.add_parser(
        "interactive",
        help="대화형 메뉴 (기본값)",
        description="자동 보정 / 세부 조정 / 설정 읽기를 메뉴로 선택하는 대화형 모드.",
    )
    p_calibrate = sub.add_parser(
        "calibrate",
        aliases=["auto-calibrate", "auto"],
        help="여러 센서를 동시에 연결해 일괄 자동 보정(공간 학습)",
        description=(
            "여러 MS605를 한꺼번에 연결해 일괄로 자동 보정(공간 학습)합니다. 기본은 스캔 후 "
            "메뉴에서 보정할 센서를 다중/전체 선택해 연결하고, 공간을 비운 뒤 Enter를 누르면 "
            "선택한 모든 센서에서 동시에 tag52=4(SPACE_LEARNING)를 보내 tag62 완료 푸시를 "
            "기다립니다(각 최대 ~3분). --collect 는 스캔 메뉴 대신 버튼을 누르는 대로 센서를 "
            "자동 수집·연결하고, --schedule HH:MM 은 지정 시각까지 링크를 keep-alive로 유지하다 "
            "무인으로 발사합니다. 실시간 레이더 스트림은 표시하지 않고(상태/결과만), 성공한 "
            "센서는 반영된 임계값을 cal_results/calibration_history.jsonl 에 저장합니다 "
            "(소스 checkout은 저장소 루트, 설치본은 사용자 데이터 디렉터리, MS605_DATA_DIR 로 변경 가능)."
        ),
    )
    p_calibrate.add_argument(
        "--collect",
        action="store_true",
        help="스캔 메뉴 대신, 버튼을 누르는 대로 센서를 자동 수집·연결 (Enter로 완료)",
    )
    p_calibrate.add_argument(
        "--schedule",
        metavar="HH:MM",
        default=None,
        help="지정 시각(24시간제)까지 연결을 유지하다 무인으로 일괄 보정 발사 "
        "(이미 지난 시각이면 다음날). 생략 시 Enter로 즉시 발사",
    )
    p_calibrate.add_argument(
        "--keepalive-interval",
        type=float,
        default=15.0,
        help="발사 대기/예약 대기 중 keep-alive 간격(초), 기본 15",
    )
    p_calibrate.add_argument(
        "--calibration-timeout",
        type=float,
        default=200.0,
        help="센서별 보정 결과(tag62) 대기 타임아웃(초), 기본 200 (~3분 + 여유)",
    )
    p_calibrate.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="모든 출력을 이 파일에도 기록 (무인 --schedule 실행 다음날 확인용)",
    )
    p_clone = sub.add_parser(
        "clone",
        help="한 센서의 설정을 읽어 다른 센서(들)에 복제",
        description=(
            "한 센서(소스)의 쓰기 가능한 설정을 읽어 프로파일로 만든 뒤, 선택한 항목만 하나 이상의 "
            "대상 센서에 그대로 적용합니다. 소스는 스캔 메뉴/--source 로 고르고, 대상은 스캔 후 "
            "메뉴에서 다중/전체 선택합니다. 프로파일은 --save PATH 로 JSON 파일에 저장하거나 "
            "--from-file PATH 로 불러올 수 있어 백업/프리셋 재사용도 됩니다. 복제 항목은 기본 전체이며 "
            "대화형 선택 또는 --only/--skip 으로 고를 수 있습니다. 항목 키: "
            + ", ".join(PROFILE_SECTION_KEYS)
            + " (거리 tag53 은 기기 고정값이라 제외, 감지모드가 SPACE_LEARNING(4) 이면 보정 트리거를 "
            "막기 위해 건너뜁니다)."
        ),
    )
    p_clone.add_argument(
        "--source",
        metavar="ADDR",
        default=None,
        help="소스(설정을 읽어올) 센서의 주소/UUID 또는 이름. 생략 시 스캔 후 선택(1대면 자동)",
    )
    p_clone.add_argument(
        "--save",
        metavar="PATH",
        default=None,
        help="읽어온 설정을 JSON으로 저장. 개인정보 보호를 위해 Git이 무시하는 private-profiles/ 권장",
    )
    p_clone.add_argument(
        "--from-file",
        metavar="PATH",
        default=None,
        help="소스 기기 대신 이 JSON 프로파일 파일에서 설정을 불러와 적용",
    )
    p_clone.add_argument(
        "--only",
        metavar="SECTIONS",
        default=None,
        help="이 항목들만 복제 (쉼표/공백 구분, 예: zone_thresholds,sensitivity)",
    )
    p_clone.add_argument(
        "--skip",
        metavar="SECTIONS",
        default=None,
        help="이 항목들을 제외하고 복제 (--only 와 동시 사용 불가)",
    )
    p_clone.add_argument(
        "--no-apply",
        action="store_true",
        help="소스 읽기/저장만 하고 대상 적용은 건너뜀 (--save 로 백업만 뜰 때)",
    )
    p_clone.add_argument(
        "-y",
        "--yes",
        action="store_true",
        dest="yes",
        help="대상 적용 전 확인 프롬프트를 생략",
    )
    p_read = sub.add_parser(
        "read",
        help="현재 설정을 읽어 출력",
        description="기기 헤더(ID/펌웨어/배터리/조도)와 민감도·모드·존 활성화·거리별 임계값을 출력합니다.",
    )
    p_zone = sub.add_parser(
        "set-zone",
        help="7개 존의 (trigger,maintain) 임계값을 설정",
        description=(
            "7개 존(0..6, 근거리→원거리)의 Presence Trigger/Maintain 임계값을 tag51에 씁니다. "
            "쓰기 직후 기기가 실제로 반영했는지 폴링으로 확인합니다(apply-lag 대응)."
        ),
    )
    p_zone.add_argument(
        "pairs",
        nargs=7,
        metavar="TRIGGER,MAINTAIN",
        type=_parse_zone_pair,
        help="정확히 7개의 'trigger,maintain' 쌍 (예: 95,40), 존 0..6 순서",
    )
    p_sens = sub.add_parser(
        "set-sensitivity",
        help="민감도(tag61)를 설정",
        description="radar 민감도(tag61)를 설정합니다. 1=LOW 2=MEDIUM 3=HIGH 4=CUSTOM.",
    )
    p_sens.add_argument(
        "level", type=int, choices=[1, 2, 3, 4], help="1=LOW 2=MEDIUM 3=HIGH 4=CUSTOM"
    )
    p_read_dnd = sub.add_parser("read-dnd", help="방해금지(DND, tag32) 상태 읽기")
    p_dnd = sub.add_parser("set-dnd", help="방해금지(DND, tag32) 상태 쓰기")
    p_dnd.add_argument("state", type=int, choices=[0, 1], help="1=on 0=off")
    p_read_pir = sub.add_parser("read-pir", help="PIR 상태(tag56) 읽기")
    p_read_subsensor = sub.add_parser(
        "read-subsensor-status", help="서브센서별 재실 상태(tag64) 읽기"
    )
    p_sync_time = sub.add_parser("sync-time", help="디바이스 시계를 호스트 UTC 시각으로 동기화(tag33)")
    p_hist = sub.add_parser(
        "read-history",
        help="이력 읽기 (presence|light, 미검증 기능)",
        description="tag57-60 이력 읽기. 실기 미검증 실험 기능입니다.",
    )
    p_hist.add_argument("kind", choices=["presence", "light"])
    p_hist.add_argument("--detail", action="store_true", help="presence: 37바이트 상세 레코드 형식 요청")
    p_monitor = sub.add_parser(
        "monitor",
        help="실시간 감지값 모니터링 (PIR/레이더/센서별/구역별)",
        description="tag55/56 push를 그대로 표시합니다: PIR 상태, 구역별 레이더 현재값/임계값, "
        "센서별 최종 재실 판정, 구역별 재실 여부. 종료하려면 Enter를 누르세요.",
    )
    p_zone_enable = sub.add_parser(
        "set-zone-enable",
        help="구역 활성화(감지 range 포함 여부, tag50) 설정",
        description="7개 구역 중 감지 range에 포함할 구역을 대화형으로 선택합니다.",
    )
    p_sensor_zones = sub.add_parser(
        "set-subsensor-zones",
        help="센서별 구역 지정 (Sensor1/2/3, tag48)",
        description="Sensor1/2/3에 각각 어떤 구역(0-6)을 배정할지 대화형으로 설정합니다.",
    )
    p_sensor_timing = sub.add_parser(
        "set-subsensor-timing",
        help="센서별 Presence/Absence Duration 설정 (tag49)",
        description="Sensor1/2/3 각각의 presence/absence 지속시간(초)을 대화형으로 설정합니다.",
    )

    # --address/--device/--scan-secs/--connect-timeout are global options, but
    # argparse only lets an *optional* flag land before the subcommand token
    # unless the subparser also declares it. Mirror them onto every subparser
    # (with suppressed defaults) so `ms605 calibrate --device X` works the
    # same as `ms605 --device X calibrate`.
    for p_sub in (
        p_interactive, p_calibrate, p_clone, p_read, p_zone, p_sens, p_read_dnd, p_dnd,
        p_read_pir, p_read_subsensor, p_sync_time, p_hist, p_monitor,
        p_zone_enable, p_sensor_zones, p_sensor_timing,
    ):
        _add_target_args(p_sub, suppress_defaults=True)
    add_gui_parser(sub)

    return ap


def _add_target_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Add --address/--device, --scan-secs, --connect-timeout to `parser`.

    When `suppress_defaults` is set (used for subparsers), unset options are
    omitted from the parsed namespace entirely instead of writing back their
    default -- otherwise argparse's subparser merge would clobber a value
    already supplied before the subcommand (e.g. `ms605 --device X read`).
    """
    unset = argparse.SUPPRESS
    parser.add_argument(
        "--address",
        "--device",
        dest="address",
        default=unset if suppress_defaults else None,
        metavar="ADDR",
        help="특정 MS605만 대상으로 (스캔 결과 중 이 주소/UUID 또는 이름이 나오면 여러 대여도 즉시 선택). "
        "예: --address <DEVICE-UUID>",
    )
    parser.add_argument(
        "--scan-secs",
        type=float,
        default=unset if suppress_defaults else 6.0,
        help="BLE 스캔 시간(초), 기본 6",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=unset if suppress_defaults else 12.0,
        help="연결 타임아웃(초), 기본 12",
    )


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    scan_secs, ct, addr = args.scan_secs, args.connect_timeout, args.address
    cmd = args.command
    if cmd in (None, "interactive"):
        coro = run_interactive(scan_secs, ct, prefer_address=addr)
    elif cmd in ("calibrate", "auto-calibrate", "auto"):
        coro = run_batch_calibration(
            scan_secs=scan_secs,
            connect_timeout=ct,
            collect=getattr(args, "collect", False),
            schedule=getattr(args, "schedule", None),
            keepalive_interval=getattr(args, "keepalive_interval", 15.0),
            calibration_timeout=getattr(args, "calibration_timeout", 200.0),
            log_file=getattr(args, "log_file", None),
            prefer_address=addr,
        )
    elif cmd == "clone":
        coro = run_clone(
            scan_secs=scan_secs,
            connect_timeout=ct,
            source_address=getattr(args, "source", None) or addr,
            save_path=getattr(args, "save", None),
            from_file=getattr(args, "from_file", None),
            only=getattr(args, "only", None),
            skip=getattr(args, "skip", None),
            no_apply=getattr(args, "no_apply", False),
            assume_yes=getattr(args, "yes", False),
        )
    elif cmd == "read":
        coro = _run_flow(
            scan_secs, ct, flow_read_config,
            show_header=True, auto_select=True, prefer_address=addr,
        )
    elif cmd == "set-zone":
        coro = _run_flow(
            scan_secs, ct, lambda session: flow_set_zone(session, args.pairs),
            show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "set-sensitivity":
        coro = _run_flow(
            scan_secs, ct, lambda session: flow_set_sensitivity(session, args.level),
            show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "read-dnd":
        coro = _run_flow(scan_secs, ct, flow_read_dnd, show_header=False, auto_select=True, prefer_address=addr)
    elif cmd == "set-dnd":
        coro = _run_flow(
            scan_secs, ct, lambda session: flow_set_dnd(session, bool(args.state)),
            show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "read-pir":
        coro = _run_flow(scan_secs, ct, flow_read_pir, show_header=False, auto_select=True, prefer_address=addr)
    elif cmd == "read-subsensor-status":
        coro = _run_flow(
            scan_secs, ct, flow_read_subsensor_status, show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "sync-time":
        coro = _run_flow(scan_secs, ct, flow_sync_time, show_header=False, auto_select=True, prefer_address=addr)
    elif cmd == "read-history":
        coro = _run_flow(
            scan_secs, ct, lambda session: flow_read_history(session, args.kind, args.detail),
            show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "monitor":
        coro = _run_flow(scan_secs, ct, flow_live_monitor, show_header=False, auto_select=True, prefer_address=addr)
    elif cmd == "set-zone-enable":
        coro = _run_flow(scan_secs, ct, flow_zone_enable, show_header=False, auto_select=True, prefer_address=addr)
    elif cmd == "set-subsensor-zones":
        coro = _run_flow(
            scan_secs, ct, flow_subsensor_zones, show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "set-subsensor-timing":
        coro = _run_flow(
            scan_secs, ct, flow_subsensor_timing, show_header=False, auto_select=True, prefer_address=addr,
        )
    elif cmd == "gui":
        coro = run_gui(args, scan_secs=scan_secs, connect_timeout=ct)
    else:  # pragma: no cover - argparse restricts choices to the above
        build_parser().print_help()
        return 2

    try:
        code = asyncio.run(coro)
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        code = 130
    # ainput() reads stdin on a worker thread that a blocking readline() won't
    # release on Ctrl-C; the interpreter would then wait up to 300s to join it on
    # exit. Cleanup already ran in run_interactive's finally, so flush and hard-
    # exit to guarantee the CLI never hangs on the way out.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
