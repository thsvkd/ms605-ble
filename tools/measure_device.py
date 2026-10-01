#!/usr/bin/env python3
"""tools/measure_device.py -- guided real-hardware measurements for the MS605.

Answers the "real-device measurements" list in docs/GUI_PLAN.md (M0). A human
runs it next to one or more sensors; it prompts (in Korean, like the CLI) for
button presses and writes one JSON results file. Each measurement is a
subcommand:

  connect-window  how long after a button press the sensor still accepts a
                  connection (repeated scan + connect/disconnect attempts)
  idle-drop       how long an idle link (no writes) survives
  ping-interval   largest keep-alive ping interval that keeps the link alive
                  (ascending ladder, then bisection between last ok / first fail)
  zone-write      does writing tag51 (zone thresholds) flip tag61 sensitivity
                  to CUSTOM(4)? Snapshots the config first and restores it in
                  a `finally` (before/after printed)
  capacity        how many sensors can be held connected at once (connect every
                  advertising sensor with keep-alive pings; first failure)
  device-id       read tag30 and check uniqueness / stability across runs
  calibration     (opt-in, long, OVERWRITES thresholds) auto-calibration with
                  keep-alive pings; records whether it succeeds
  all             device-id, zone-write, idle-drop, ping-interval,
                  connect-window, capacity; `--calibration` adds calibration

Privacy: the repo and results are meant to be shareable, so device addresses,
names and tag30 ids are NEVER written to the JSON or printed in measurement
output -- only a short sha256 prefix -- unless `--show-ids` is given. (Short
hashes of low-entropy ids are not secret against brute force; treat the results
file as "pseudonymous", not anonymous.) Error text is scrubbed of anything that
looks like an address too. Results accumulate in `--out` (default
cal_results/measure_results.json under the checkout, a git-ignored directory
so the file is not committed by accident): every run is appended, which is what
lets device-id compare ids across runs.

Run (press each sensor's button when prompted):
    uv run python tools/measure_device.py device-id --out cal_results/device_id.json
    uv run python tools/measure_device.py connect-window --rounds 5
    uv run python tools/measure_device.py all --calibration
The hardware parts cannot run without sensors; the pure helpers (hashing /
redaction, result aggregation, interval search) are unit-tested in
tests/test_measure_device.py.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ms605 import MS605, MS605ConnectionError, MS605Error, MS605TimeoutError
from ms605.protocol import TAG_DEVICE_ID, Sensitivity

RESULTS_SCHEMA = 1
HASH_LEN = 8
# In the git-ignored cal_results/ (like the calibration history): the file
# holds brute-forceable id hashes, or raw ids with --show-ids.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(_REPO_ROOT, "cal_results", "measure_results.json")
DEFAULT_LADDER = (5, 10, 15, 20, 30, 45, 60)

# -- pure helpers: hashing / redaction -------------------------------------

# MAC (":" "-" or BlueZ "_" separated), CoreBluetooth UUID, and the sensor's
# advertised name (the suffix is device-specific).
_ADDRESS_RES = (
    re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?:[:_-][0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f])"),
    re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"),
    re.compile(r"\b(?:RFBL|MRBL)_\w+"),
)


def short_hash(value: bytes | str) -> str:
    """Stable short sha256 prefix used in place of an address / name / id."""
    data = value.encode() if isinstance(value, str) else bytes(value)
    return hashlib.sha256(data).hexdigest()[:HASH_LEN]


def redact(value: bytes | str, show_ids: bool) -> str:
    """The value itself (hex for bytes) with --show-ids, else its short hash."""
    if show_ids:
        return value.hex() if isinstance(value, (bytes, bytearray)) else value
    return short_hash(value)


def scrub_text(text: str) -> str:
    for pattern in _ADDRESS_RES:
        text = pattern.sub("<addr>", text)
    return text


def scrub_obj(obj: Any) -> Any:
    """Recursively scrub every string in a JSON-able structure (error text from
    bleak/BlueZ routinely embeds the device address)."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


# -- pure helpers: aggregation ---------------------------------------------


def summarize_durations(values: Sequence[float | None]) -> dict[str, Any]:
    """count/min/median/max of the non-None values (None = "did not happen")."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0, "min_s": None, "median_s": None, "max_s": None}
    return {
        "count": len(vals),
        "min_s": round(min(vals), 2),
        "median_s": round(statistics.median(vals), 2),
        "max_s": round(max(vals), 2),
    }


@dataclass
class Attempt:
    """One connect attempt `t` seconds after the button press."""

    t: float
    ok: bool
    error: str | None = None


def analyze_attempts(attempts: Sequence[Attempt], first_advert_s: float | None) -> dict[str, Any]:
    """Collapse one button-press round: when the first connect worked, when the
    last one did, when it first failed afterwards, and the advert-to-last-success
    window (= how long the sensor stayed connectable)."""
    oks = [a for a in attempts if a.ok]
    first_ok = oks[0].t if oks else None
    last_ok = oks[-1].t if oks else None
    first_fail_after = None
    if first_ok is not None:
        first_fail_after = next((a.t for a in attempts if not a.ok and a.t > first_ok), None)
    window = None
    if first_advert_s is not None and last_ok is not None:
        window = last_ok - first_advert_s
    return {
        "first_advert_s": first_advert_s,
        "first_success_s": first_ok,
        "last_success_s": last_ok,
        "first_failure_after_success_s": first_fail_after,
        "window_s": window,
        "ok_count": len(oks),
        "fail_count": len(attempts) - len(oks),
    }


def should_stop_window(attempts: Sequence[Attempt], stop_after: int) -> bool:
    """True once the window has clearly closed: at least one success, then
    `stop_after` consecutive failures."""
    if not any(a.ok for a in attempts):
        return False
    trailing = 0
    for a in reversed(attempts):
        if a.ok:
            break
        trailing += 1
    return trailing >= stop_after


@dataclass
class IntervalTrial:
    interval: int  # seconds between pings
    ok: bool  # link survived the whole hold time with every ping acked
    held_s: float  # how long it survived
    reason: str | None = None


def parse_ladder(text: str) -> list[int]:
    try:
        values = sorted({int(p) for p in text.split(",") if p.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ladder must be comma-separated integers: {text!r}") from exc
    if not values or values[0] < 1:
        raise argparse.ArgumentTypeError(f"ladder needs at least one positive interval: {text!r}")
    return values


def next_interval(ladder: Sequence[int], trials: Sequence[IntervalTrial], resolution: int = 5) -> int | None:
    """Next ping interval to try, or None when the search is done.

    Walk `ladder` upwards while every trial passes; at the first failure bisect
    between the largest passing and smallest failing interval until they are
    within `resolution` seconds. Stops early if even the smallest interval
    fails, or if results contradict each other (a passing interval above a
    failing one -- flaky link; see summarize_intervals)."""
    ok = [t.interval for t in trials if t.ok]
    bad = [t.interval for t in trials if not t.ok]
    max_ok = max(ok, default=None)
    min_bad = min(bad, default=None)
    if min_bad is None:
        return next((iv for iv in ladder if max_ok is None or iv > max_ok), None)
    if max_ok is None or max_ok >= min_bad or min_bad - max_ok <= resolution:
        return None
    mid = (max_ok + min_bad) // 2
    return mid if max_ok < mid < min_bad else None


def summarize_intervals(trials: Sequence[IntervalTrial]) -> dict[str, Any]:
    ok = [t.interval for t in trials if t.ok]
    bad = [t.interval for t in trials if not t.ok]
    max_ok = max(ok, default=None)
    min_bad = min(bad, default=None)
    return {
        "max_safe_interval_s": max_ok,
        "min_failing_interval_s": min_bad,
        "consistent": max_ok is None or min_bad is None or max_ok < min_bad,
        "trials": [asdict(t) for t in trials],
    }


def describe_flip(base_sensitivity: int, after_sensitivity: int) -> str:
    """Classify tag61 before/after a tag51 write."""
    if after_sensitivity == base_sensitivity:
        return "unchanged"
    if after_sensitivity == int(Sensitivity.CUSTOM):
        return "flipped_to_custom"
    return "changed_other"


def perturb_thresholds(pairs: Sequence[tuple[int, int]], delta: int = 1) -> list[tuple[int, int]]:
    """Shift every (trigger, maintain) by `delta`, clamped to the u16 range --
    a small, easily-restored change that is guaranteed to differ from `pairs`
    unless both ends of a pair are already pinned at the clamp."""
    step = delta if delta else 1

    def clamp(v: int) -> int:
        return max(0, min(0xFFFF, v))

    shifted = [(clamp(t + step), clamp(m + step)) for t, m in pairs]
    if shifted == [tuple(p) for p in pairs]:  # pinned at the top: go the other way
        shifted = [(clamp(t - step), clamp(m - step)) for t, m in pairs]
    return shifted


def collect_id_observations(doc: dict[str, Any]) -> list[dict[str, str]]:
    """All (address_hash, id_hash) pairs recorded by device-id runs in a results
    document."""
    out = []
    for run in doc.get("runs", []):
        if run.get("measurement") != "device-id":
            continue
        for dev in run.get("result", {}).get("devices", []):
            if dev.get("address_hash") and dev.get("id_hash"):
                out.append({"address_hash": dev["address_hash"], "id_hash": dev["id_hash"]})
    return out


def analyze_device_ids(observations: Sequence[dict[str, str]]) -> dict[str, Any]:
    """Uniqueness (no two different sensors share an id) and stability (one
    sensor always reports the same id) over every observation so far."""
    ids_by_addr: dict[str, set[str]] = {}
    addrs_by_id: dict[str, set[str]] = {}
    for o in observations:
        ids_by_addr.setdefault(o["address_hash"], set()).add(o["id_hash"])
        addrs_by_id.setdefault(o["id_hash"], set()).add(o["address_hash"])
    unstable = sorted(a for a, ids in ids_by_addr.items() if len(ids) > 1)
    shared = sorted(i for i, addrs in addrs_by_id.items() if len(addrs) > 1)
    return {
        "observations": len(observations),
        "distinct_devices": len(ids_by_addr),
        "distinct_ids": len(addrs_by_id),
        "unique": not shared,
        "stable": not unstable,
        "shared_id_hashes": shared,
        "unstable_address_hashes": unstable,
    }


# -- pure helpers: results file ---------------------------------------------


def load_results(path: str) -> dict[str, Any]:
    """Existing results document, or a fresh one. A corrupt file is an error --
    never silently overwritten."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"schema": RESULTS_SCHEMA, "runs": []}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"결과 파일이 손상되었습니다 ({path}): {exc}. 다른 --out 경로를 쓰세요.") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
        raise SystemExit(f"결과 파일 형식이 올바르지 않습니다: {path}")
    return doc


def add_run(
    doc: dict[str, Any], measurement: str, started: str, show_ids: bool, result: dict[str, Any]
) -> dict[str, Any]:
    """Append one measurement run (scrubbed of address-like text unless
    `show_ids`) and return the document."""
    doc.setdefault("runs", []).append(
        {
            "measurement": measurement,
            "started": started,
            "show_ids": show_ids,
            "result": result if show_ids else scrub_obj(result),
        }
    )
    return doc


def save_results(path: str, doc: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# -- hardware plumbing --------------------------------------------------------


@dataclass
class Ctx:
    out: str
    show_ids: bool
    scan_secs: float
    connect_timeout: float
    address: str | None

    def label(self, dev: Any) -> str:
        if self.show_ids:
            return f"{dev.name or '(이름없음)'} {dev.address}"
        return f"센서#{short_hash(dev.address)}"

    def exc(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        return text if self.show_ids else scrub_text(text)

    def matches(self, dev: Any) -> bool:
        if self.address is None:
            return True
        want = self.address.strip().lower()
        return dev.address.lower() == want or (dev.name or "").lower() == want


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_run(ctx: Ctx, measurement: str, started: str, result: dict[str, Any]) -> None:
    doc = add_run(load_results(ctx.out), measurement, started, ctx.show_ids, result)
    save_results(ctx.out, doc)
    say(f"\n결과 저장: {ctx.out} ({measurement})")


async def scan_all(ctx: Ctx, rounds: int = 3) -> list[Any]:
    """Union of several scans (adverts are intermittent), filtered by --address."""
    seen: dict[str, Any] = {}
    for i in range(rounds):
        say(f"  스캔 {i + 1}/{rounds} ...")
        for dev in await MS605.scan(timeout=ctx.scan_secs):
            if ctx.matches(dev):
                seen[dev.address] = dev
    return list(seen.values())


async def pick_device(ctx: Ctx) -> Any:
    """One advertising sensor: automatic if exactly one, else a numbered menu."""
    while True:
        say("MS605 검색 중... 센서의 버튼을 눌러 광고 상태로 만드세요.")
        devices = [d for d in await MS605.scan(timeout=ctx.scan_secs) if ctx.matches(d)]
        if not devices:
            say("  보이는 센서가 없습니다. 다시 검색합니다.")
            continue
        if len(devices) == 1:
            say(f"  {ctx.label(devices[0])} 선택")
            return devices[0]
        for i, dev in enumerate(devices, 1):
            say(f"  {i}) {ctx.label(dev)}")
        choice = await ask("측정할 센서 번호 (r = 다시 검색): ")
        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            return devices[int(choice) - 1]


async def connect_prompted(ctx: Ctx, device: Any, *, press_first: bool = False) -> tuple[MS605, Any]:
    """Connect, asking the human to press the button before each (re)try.
    Returns (ms, possibly re-scanned device handle); raises MS605ConnectionError
    if the user quits with 'q'."""
    target = device.address
    need_press = press_first
    while True:
        if need_press:
            if (await ask("센서 버튼을 누른 뒤 Enter (중단: q): ")).lower() == "q":
                raise MS605ConnectionError("사용자가 연결 재시도를 중단했습니다")
            for d in await MS605.scan(timeout=ctx.scan_secs):  # fresh handle after a press
                if d.address == target:
                    device = d
                    break
        ms = MS605(device)
        try:
            await ms.connect(timeout=ctx.connect_timeout)
            return ms, device
        except Exception as exc:  # noqa: BLE001 - any BLE failure => ask for a press
            say(f"  연결 실패: {ctx.exc(exc)}")
            need_press = True


async def safe_disconnect(ms: MS605 | None) -> None:
    if ms is not None:
        try:
            await ms.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup of a possibly dead link
            pass


async def _ping_loop(ms: MS605, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        if ms.is_connected:
            try:
                await ms.ping()
            except Exception:  # noqa: BLE001 - a dead link shows up via is_connected
                pass


# -- (1) connect window -------------------------------------------------------


async def measure_connect_window(
    ctx: Ctx,
    *,
    rounds: int,
    attempt_interval: float,
    attempt_timeout: float,
    max_window: float,
    advert_wait: float,
    stop_after: int,
) -> dict[str, Any]:
    device = await pick_device(ctx)
    out_rounds: list[dict[str, Any]] = []
    for r in range(1, rounds + 1):
        say(f"\n[{r}/{rounds}] 센서의 버튼을 누르는 즉시 Enter 를 누르세요 (타이머 시작)")
        await ask("  ")
        t0 = time.monotonic()
        first_advert: float | None = None
        attempts: list[Attempt] = []
        while time.monotonic() - t0 < advert_wait:
            found = await MS605.scan(timeout=1.0)  # advert timestamp resolution ~1s
            hit = next((d for d in found if d.address == device.address), None)
            if hit is not None:
                device, first_advert = hit, time.monotonic() - t0
                say(f"  첫 광고 감지: {first_advert:.1f}s")
                break
        if first_advert is None:
            say(f"  {advert_wait:.0f}s 안에 광고가 보이지 않았습니다.")
        while first_advert is not None and time.monotonic() - t0 < max_window:
            t = time.monotonic() - t0
            ms = MS605(device)
            try:
                await ms.connect(timeout=attempt_timeout)
                attempts.append(Attempt(round(t, 2), True))
                say(f"  t={t:6.1f}s 연결 성공")
            except Exception as exc:  # noqa: BLE001 - failure is the data point
                attempts.append(Attempt(round(t, 2), False, ctx.exc(exc)))
                say(f"  t={t:6.1f}s 연결 실패")
            finally:
                await safe_disconnect(ms)
            if should_stop_window(attempts, stop_after):
                break
            await asyncio.sleep(attempt_interval)
        analysis = analyze_attempts(attempts, first_advert)
        say(f"  -> 연결 가능 구간: {analysis['window_s']}s (첫 광고 ~ 마지막 성공)")
        out_rounds.append({**analysis, "attempts": [asdict(a) for a in attempts]})
    return {
        "device": redact(device.address, ctx.show_ids),
        "attempt_interval_s": attempt_interval,
        "rounds": out_rounds,
        "window_s": summarize_durations([x["window_s"] for x in out_rounds]),
        "first_success_s": summarize_durations([x["first_success_s"] for x in out_rounds]),
    }


# -- (2) idle drop / max safe ping interval -------------------------------------


async def measure_idle_drop(ctx: Ctx, *, repeat: int, max_idle: float) -> dict[str, Any]:
    device = await pick_device(ctx)
    drops: list[float | None] = []
    for i in range(1, repeat + 1):
        ms, device = await connect_prompted(ctx, device, press_first=i > 1)
        try:
            say(f"[{i}/{repeat}] 연결됨 -- 트래픽 없이 대기 (최대 {max_idle:.0f}s)")
            t0 = time.monotonic()
            dropped: float | None = None
            next_report = 15.0
            while time.monotonic() - t0 < max_idle:
                await asyncio.sleep(0.5)
                elapsed = time.monotonic() - t0
                if not ms.is_connected:
                    dropped = elapsed
                    break
                if elapsed >= next_report:
                    say(f"  {elapsed:.0f}s 경과, 연결 유지 중")
                    next_report += 15.0
            say("  -> " + (f"{dropped:.1f}s 에 끊김" if dropped is not None else "끊기지 않음 (최대 대기 도달)"))
            drops.append(round(dropped, 2) if dropped is not None else None)
        finally:
            await safe_disconnect(ms)
    return {
        "max_idle_s": max_idle,
        "drop_times_s": drops,
        "survived_max_count": sum(d is None for d in drops),
        "summary": summarize_durations(drops),
    }


async def run_ping_trial(ctx: Ctx, ms: MS605, interval: int, hold: float) -> IntervalTrial:
    t0 = time.monotonic()
    next_ping = float(interval)
    while True:
        now = time.monotonic() - t0
        if not ms.is_connected:
            return IntervalTrial(interval, False, round(now, 2), "link_dropped")
        if now >= hold:
            return IntervalTrial(interval, True, round(now, 2))
        if now >= next_ping:
            try:
                await ms.ping()
            except MS605TimeoutError:
                if ms.is_connected:  # one lost/bad-CRC ACK on a live link is not a drop; keep holding
                    say(f"  (ACK 유실 — 링크는 유지됨, {interval}s 간격 계속)")
            except Exception as exc:  # noqa: BLE001 - failed ping write == link dead
                return IntervalTrial(interval, False, round(now, 2), f"ping_failed: {ctx.exc(exc)}")
            next_ping += interval
        await asyncio.sleep(0.25)


async def measure_ping_interval(
    ctx: Ctx, *, ladder: Sequence[int], resolution: int, hold_factor: float, min_hold: float
) -> dict[str, Any]:
    device = await pick_device(ctx)
    trials: list[IntervalTrial] = []
    aborted: str | None = None
    first = True
    while (interval := next_interval(ladder, trials, resolution)) is not None:
        hold = max(min_hold, hold_factor * interval)
        try:
            ms, device = await connect_prompted(ctx, device, press_first=not first)
        except MS605ConnectionError as exc:
            aborted = ctx.exc(exc)
            break
        first = False
        try:
            say(f"\nping {interval}s 간격으로 {hold:.0f}s 유지 시도 ...")
            trial = await run_ping_trial(ctx, ms, interval, hold)
        finally:
            await safe_disconnect(ms)
        say(f"  -> {'유지됨' if trial.ok else '끊김: ' + str(trial.reason)} ({trial.held_s}s)")
        trials.append(trial)
    return {**summarize_intervals(trials), "ladder": list(ladder), "hold_factor": hold_factor, "aborted": aborted}


# -- (3) zone write vs sensitivity ------------------------------------------------


@dataclass
class ConfigSnapshot:
    sensitivity: int
    thresholds: list[tuple[int, int]]

    @classmethod
    def from_config(cls, cfg: Any) -> ConfigSnapshot:
        return cls(int(cfg.sensitivity), [(z.trigger, z.maintain) for z in cfg.zone_thresholds])


async def read_snapshot(ms: MS605) -> ConfigSnapshot:
    return ConfigSnapshot.from_config(await ms.read_config())


def print_snapshot(title: str, snap: ConfigSnapshot) -> None:
    say(f"  {title}: 민감도={snap.sensitivity} 임계값={snap.thresholds}")


async def restore_config(ms: MS605, snap: ConfigSnapshot) -> dict[str, Any]:
    """Put sensitivity (tag61) and thresholds (tag51) back to `snap`. Writing a
    preset sensitivity reloads that preset's thresholds and writing tag51 may
    flip sensitivity to CUSTOM, so verify by re-reading and retry once."""
    state = None
    for _ in range(2):
        await ms.set_sensitivity(snap.sensitivity)
        now = await read_snapshot(ms)
        if now.thresholds != snap.thresholds:
            await ms.set_zone_thresholds(snap.thresholds)
            now = await read_snapshot(ms)
        state = now
        if now == snap:
            break
    assert state is not None
    return {
        "sensitivity_restored": state.sensitivity == snap.sensitivity,
        "thresholds_restored": state.thresholds == snap.thresholds,
        "final": asdict(state),
    }


async def measure_zone_write(ctx: Ctx, *, delays: Sequence[float]) -> dict[str, Any]:
    device = await pick_device(ctx)
    ms, device = await connect_prompted(ctx, device)
    snapshot: ConfigSnapshot | None = None
    result: dict[str, Any] = {}
    try:
        snapshot = await read_snapshot(ms)
        result["original"] = asdict(snapshot)
        say("\n[원본 설정 스냅샷 -- 종료 시 복원합니다]")
        print_snapshot("전", snapshot)

        base = snapshot.sensitivity
        if base == int(Sensitivity.CUSTOM):
            # a CUSTOM start cannot show a flip: switch to MEDIUM first (restored later)
            say("  원본이 CUSTOM(4) 이라 MEDIUM(2) 로 바꿔 기준을 만듭니다.")
            await ms.set_sensitivity(Sensitivity.MEDIUM)
            base = int(Sensitivity.MEDIUM)
        before = await read_snapshot(ms)
        result["baseline"] = asdict(before)

        # A: tag51 alone, then watch tag61 (and the thresholds) settle
        target = perturb_thresholds(before.thresholds)
        say("\n[실험 A] tag51 만 쓰고 민감도(tag61) 확인")
        await ms.set_zone_thresholds(target)
        a_obs = []
        for d in delays:
            await asyncio.sleep(d)
            snap = await read_snapshot(ms)
            a_obs.append(
                {
                    "after_s": d,
                    "sensitivity": snap.sensitivity,
                    "thresholds_match": snap.thresholds == target,
                    "flip": describe_flip(base, snap.sensitivity),
                }
            )
            say(f"  +{d}s: 민감도={snap.sensitivity} ({a_obs[-1]['flip']}) 임계값 반영={a_obs[-1]['thresholds_match']}")
        result["write_thresholds"] = a_obs
        result["flipped_to_custom"] = any(o["flip"] == "flipped_to_custom" for o in a_obs)

        # B: write order -- does re-writing a preset sensitivity clobber the custom thresholds?
        say("\n[실험 B] 이어서 프리셋 민감도를 다시 쓰면 임계값이 덮어써지는가")
        await ms.set_sensitivity(base)
        snap = await read_snapshot(ms)
        result["sensitivity_after_thresholds"] = {
            "sensitivity": snap.sensitivity,
            "thresholds_kept": snap.thresholds == target,
        }
        say(f"  민감도={snap.sensitivity} 사용자 임계값 유지={snap.thresholds == target}")
    finally:
        say("\n[원본 설정 복원 중]")
        try:
            if not ms.is_connected:
                await safe_disconnect(ms)
                ms, device = await connect_prompted(ctx, device, press_first=True)
            if snapshot is not None:
                result["restore"] = await restore_config(ms, snapshot)
                print_snapshot("후", ConfigSnapshot(**result["restore"]["final"]))
                say(f"  복원 결과: {result['restore']}")
        except Exception as exc:  # noqa: BLE001 - report, never mask the original error
            result["restore"] = {"error": ctx.exc(exc)}
            say(f"  !! 복원 실패: {ctx.exc(exc)} -- 원본: {snapshot}")
        finally:
            await safe_disconnect(ms)
    return result


# -- (4) capacity ---------------------------------------------------------------------


async def measure_capacity(ctx: Ctx, *, max_devices: int, hold: float, keepalive: float) -> dict[str, Any]:
    await ask("측정할 모든 센서의 버튼을 눌러 광고 상태로 만든 뒤 Enter: ")
    devices = (await scan_all(ctx))[:max_devices]
    say(f"광고 중인 센서 {len(devices)}대 발견")
    connected: list[MS605] = []
    tasks: list[asyncio.Task] = []
    first_failure: dict[str, Any] | None = None
    drops: list[dict[str, Any]] = []
    try:
        for i, dev in enumerate(devices, 1):
            ms = MS605(dev)
            try:
                await ms.connect(timeout=ctx.connect_timeout)
            except Exception as exc:  # noqa: BLE001 - the first failure is the measurement
                first_failure = {"index": i, "error": ctx.exc(exc)}
                say(f"  {i}번째 연결 실패: {first_failure['error']}")
                break
            connected.append(ms)
            tasks.append(asyncio.create_task(_ping_loop(ms, keepalive)))
            say(f"  {i}/{len(devices)} 연결됨 ({ctx.label(dev)})")
        say(f"\n동시 연결 {len(connected)}대 -- {hold:.0f}s 유지 확인")
        t0 = time.monotonic()
        dropped: set[int] = set()
        while time.monotonic() - t0 < hold:
            await asyncio.sleep(1.0)
            for idx, ms in enumerate(connected, 1):
                if idx not in dropped and not ms.is_connected:
                    dropped.add(idx)
                    drops.append({"index": idx, "t_s": round(time.monotonic() - t0, 1)})
                    say(f"  {idx}번 링크 끊김 (t={drops[-1]['t_s']}s)")
        alive = sum(ms.is_connected for ms in connected)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ms in connected:
            await safe_disconnect(ms)
    return {
        "advertising_found": len(devices),
        "max_devices_limit": max_devices,
        "connected": len(connected),
        "first_failure": first_failure,
        "alive_after_hold": alive,
        "hold_s": hold,
        "keepalive_s": keepalive,
        "drops": drops,
    }


# -- (5) device id ------------------------------------------------------------------------


async def measure_device_id(ctx: Ctx) -> dict[str, Any]:
    await ask("측정할 센서(들)의 버튼을 눌러 광고 상태로 만든 뒤 Enter: ")
    devices = await scan_all(ctx)
    say(f"광고 중인 센서 {len(devices)}대")
    entries: list[dict[str, Any]] = []
    for dev in devices:
        entry: dict[str, Any] = {"address_hash": short_hash(dev.address)}
        ms = MS605(dev)
        try:
            await ms.connect(timeout=ctx.connect_timeout)
            value = (await ms.read_raw([TAG_DEVICE_ID])).get(TAG_DEVICE_ID)
            if value is None:
                entry["error"] = "tag30 missing from response"
            else:
                entry.update(id_hash=short_hash(value), id_len=len(value))
                if ctx.show_ids:
                    entry.update(address=dev.address, id_hex=value.hex())
        except Exception as exc:  # noqa: BLE001 - record per-device failure, keep going
            entry["error"] = ctx.exc(exc)
        finally:
            await safe_disconnect(ms)
        say(f"  {ctx.label(dev)}: id={entry.get('id_hash', '?')} {entry.get('error', '')}")
        entries.append(entry)
    doc = add_run(load_results(ctx.out), "device-id", utc_now(), ctx.show_ids, {"devices": entries})
    analysis = analyze_device_ids(collect_id_observations(doc))
    say(f"누적 분석: {analysis}")
    return {"devices": entries, "analysis_so_far": analysis}


# -- (6) calibration -------------------------------------------------------------------------


async def measure_calibration(ctx: Ctx, *, keepalive: float, assume_yes: bool, restore: bool) -> dict[str, Any]:
    say("!! 자동 보정은 존 임계값을 덮어씁니다 (수 분 소요). 테스트용 센서에서만 실행하세요.")
    if not assume_yes and (await ask("계속하려면 y: ")).lower() != "y":
        return {"skipped": "사용자가 취소"}
    device = await pick_device(ctx)
    ms, device = await connect_prompted(ctx, device)
    snapshot: ConfigSnapshot | None = None
    result: dict[str, Any] = {"keepalive_s": keepalive}
    try:
        snapshot = await read_snapshot(ms)
        result["before"] = asdict(snapshot)
        print_snapshot("보정 전", snapshot)
        say(f"보정 시작 (keep-alive {keepalive:.0f}s) ...")
        t0 = time.monotonic()
        try:
            result["success"] = await ms.start_auto_calibration(keepalive_interval=keepalive)
        except MS605Error as exc:
            result["success"] = False
            result["error"] = ctx.exc(exc)
        result["duration_s"] = round(time.monotonic() - t0, 1)
        say(f"  -> 성공={result['success']} ({result['duration_s']}s) {result.get('error', '')}")
        if ms.is_connected:
            after = await read_snapshot(ms)
            result["after"] = asdict(after)
            print_snapshot("보정 후", after)
        if restore and snapshot is not None and ms.is_connected:
            result["restore"] = await restore_config(ms, snapshot)
            say(f"  원본 복원: {result['restore']}")
    finally:
        await safe_disconnect(ms)
    return result


# -- CLI ------------------------------------------------------------------------------------------

MEASUREMENTS = {
    "connect-window": lambda ctx, a: measure_connect_window(
        ctx,
        rounds=a.rounds,
        attempt_interval=a.attempt_interval,
        attempt_timeout=a.attempt_timeout,
        max_window=a.max_window,
        advert_wait=a.advert_wait,
        stop_after=a.stop_after,
    ),
    "idle-drop": lambda ctx, a: measure_idle_drop(ctx, repeat=a.repeat, max_idle=a.max_idle),
    "ping-interval": lambda ctx, a: measure_ping_interval(
        ctx, ladder=a.ladder, resolution=a.resolution, hold_factor=a.hold_factor, min_hold=a.min_hold
    ),
    "zone-write": lambda ctx, a: measure_zone_write(ctx, delays=a.delays),
    "capacity": lambda ctx, a: measure_capacity(ctx, max_devices=a.max_devices, hold=a.hold, keepalive=a.keepalive),
    "device-id": lambda ctx, a: measure_device_id(ctx),
    "calibration": lambda ctx, a: measure_calibration(
        ctx, keepalive=a.keepalive, assume_yes=a.yes, restore=a.restore
    ),
}
ALL_ORDER = ("device-id", "zone-write", "idle-drop", "ping-interval", "connect-window", "capacity")
COMMON_DESTS = ("out", "show_ids", "scan_secs", "connect_timeout", "address")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", default=DEFAULT_OUT, help=f"results JSON (appended to; default {DEFAULT_OUT})")
    common.add_argument("--show-ids", action="store_true", help="include raw addresses/ids (default: hashes)")
    common.add_argument("--scan-secs", type=float, default=5.0)
    common.add_argument("--connect-timeout", type=float, default=10.0)
    common.add_argument("--address", help="only consider this device (address or name)")

    parser = argparse.ArgumentParser(description="MS605 guided hardware measurements (docs/GUI_PLAN.md M0)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("connect-window", parents=[common], help="how long a connection is accepted after a press")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--attempt-interval", type=float, default=2.0)
    p.add_argument("--attempt-timeout", type=float, default=4.0)
    p.add_argument("--max-window", type=float, default=120.0)
    p.add_argument("--advert-wait", type=float, default=30.0)
    p.add_argument("--stop-after", type=int, default=3, help="consecutive failures after a success that end a round")

    p = sub.add_parser("idle-drop", parents=[common], help="idle link lifetime with no traffic")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--max-idle", type=float, default=300.0)

    p = sub.add_parser("ping-interval", parents=[common], help="largest keep-alive interval that holds the link")
    p.add_argument("--ladder", type=parse_ladder, default=list(DEFAULT_LADDER))
    p.add_argument("--resolution", type=int, default=5, help="stop bisecting within this many seconds")
    p.add_argument("--hold-factor", type=float, default=3.0, help="hold each trial for factor x interval")
    p.add_argument("--min-hold", type=float, default=60.0)

    p = sub.add_parser("zone-write", parents=[common], help="does writing tag51 flip tag61 to CUSTOM (restores config)")
    p.add_argument("--delays", type=lambda s: [float(x) for x in s.split(",")], default=[0.0, 1.0, 3.0])

    p = sub.add_parser("capacity", parents=[common], help="how many sensors can be connected at once")
    p.add_argument("--max-devices", type=int, default=16)
    p.add_argument("--hold", type=float, default=30.0)
    p.add_argument("--keepalive", type=float, default=15.0)

    sub.add_parser("device-id", parents=[common], help="tag30 id uniqueness / stability (hashed)")

    p = sub.add_parser("calibration", parents=[common], help="opt-in: auto-calibration (overwrites thresholds)")
    p.add_argument("--keepalive", type=float, default=15.0)
    p.add_argument("--yes", action="store_true", help="skip the overwrite confirmation")
    p.add_argument("--restore", action="store_true", help="write the original sensitivity/thresholds back afterwards")

    p = sub.add_parser("all", parents=[common], help="run every measurement in turn")
    p.add_argument("--calibration", action="store_true", help="also run the long, opt-in calibration measurement")
    return parser


async def run_one(ctx: Ctx, name: str, args: argparse.Namespace) -> bool:
    started = utc_now()
    ok = True
    try:
        result = await MEASUREMENTS[name](ctx, args)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the run (and `all`) going, record the failure
        say(f"\n!! {name} 실패: {ctx.exc(exc)}")
        result, ok = {"error": ctx.exc(exc)}, False
    save_run(ctx, name, started, result)
    return ok


async def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    ctx = Ctx(args.out, args.show_ids, args.scan_secs, args.connect_timeout, args.address)
    load_results(ctx.out)  # a corrupt/foreign --out fails now, not after a long measurement
    if args.command != "all":
        return 0 if await run_one(ctx, args.command, args) else 1
    names = list(ALL_ORDER) + (["calibration"] if args.calibration else [])
    failures = 0
    for name in names:
        sub_args = parser.parse_args([name])  # per-measurement defaults
        for dest in COMMON_DESTS:
            setattr(sub_args, dest, getattr(args, dest))
        say(f"\n===== {name} =====")
        failures += not await run_one(ctx, name, sub_args)
    return 1 if failures else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    code = 1
    try:
        code = asyncio.run(run(args, parser))
    except KeyboardInterrupt:
        say("\n중단되었습니다.")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # bleak backends can leave non-daemon threads alive


if __name__ == "__main__":
    main()
