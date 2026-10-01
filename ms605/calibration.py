"""ms605.calibration -- one sensor's auto-calibration job, the presence
pre-flight check, and the calibration history record. See docs/CORE_API.md
section 5.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .driver import MS605
from .errors import MS605ConnectionError, MS605Error, MS605TimeoutError, StorageError
from .events import (
    TERMINAL_CALIBRATION_STATES,
    CalibrationProgress,
    CalibrationResult,
    CalibrationState,
    CalibrationStateChanged,
    Event,
    LinkState,
    LiveRadar,
    PirChanged,
)
from .models import MS605Config, zone_distances
from .protocol import CALIBRATION_TIMEOUT_S
from .session import DeviceSession
from .storage import Storage

_log = logging.getLogger(__name__)

# NOT MEASURED: docs/GUI_PLAN.md section 1 says "up to about 3 minutes"; the
# simulator assumes the same. Only the progress bar's denominator.
EXPECTED_CALIBRATION_S = 180.0
# tag55 arrives about once a second (simulator assumption) -> about 3 samples.
PREFLIGHT_WINDOW_S = 3.0

_CANCEL_DETAIL = "링크를 끊어 학습을 중단함. 다시 연결하려면 버튼을 누르세요"


@dataclass(frozen=True)
class PresenceSnapshot:
    device_id: str | None
    samples: int  # tag55 pushes received in the window
    presence: bool | None  # any sub-sensor presence in any sample; None if samples == 0
    pir: bool | None  # PIR detected at any point in the window; None if never seen
    occupied: bool | None  # presence or pir; None if both are None
    error: str | None = None


async def presence_snapshot(session: DeviceSession, *, window_s: float = PREFLIGHT_WINDOW_S) -> PresenceSnapshot:
    """Watch live output for `window_s` seconds and report whether anyone seems
    present. A warning only: it never blocks a calibration (D6)."""
    if session.state is not LinkState.CONNECTED:
        raise MS605ConnectionError("not connected")
    samples = 0
    presence: bool | None = None
    pir: bool | None = None

    def on_event(ev: Event) -> None:
        nonlocal samples, presence, pir
        if isinstance(ev, LiveRadar) and ev.address == session.address:
            samples += 1
            presence = bool(presence) or any(ev.snapshot.sub_sensor_presence)
        elif isinstance(ev, PirChanged) and ev.address == session.address:
            pir = bool(pir) or ev.detected

    unsubscribe = session.bus.subscribe(on_event)
    try:
        await session.acquire_live()
        # PirChanged fires only on change. Read after acquire_live(), which
        # forgets a value left from an earlier stream: never seed from that.
        if session.last_pir is not None:
            pir = bool(pir) or session.last_pir
        await asyncio.sleep(window_s)
    finally:
        unsubscribe()
        await session.release_live()
    if presence is None and pir is None:
        occupied = None
    else:
        occupied = bool(presence) or bool(pir)
    return PresenceSnapshot(device_id=session.device_id, samples=samples, presence=presence, pir=pir, occupied=occupied)


def build_calibration_record(
    device_name: str | None,
    address: str,
    cfg: MS605Config,
    *,
    device_id: str | None = None,
    when: datetime | None = None,
) -> dict:
    """Snapshot of what auto-calibration actually committed to the device --
    one line of the calibration history (Storage.append_history())."""
    dists = zone_distances(cfg)
    return {
        "timestamp": (when or datetime.now(timezone.utc)).isoformat(),
        "device_name": device_name,
        "device_address": address,
        "sensitivity": cfg.sensitivity,
        "detect_mode": cfg.detect_mode,
        "zones": [
            {
                "index": i,
                "distance_m": dists[i] if i < len(dists) else None,
                "trigger": zt.trigger,
                "maintain": zt.maintain,
            }
            for i, zt in enumerate(cfg.zone_thresholds)
        ],
        "device_id": device_id,
    }


def _pairs(cfg: MS605Config) -> tuple[tuple[int, int], ...]:
    return tuple((zt.trigger, zt.maintain) for zt in cfg.zone_thresholds)


class CalibrationJob:
    """Runs one auto-calibration on `session` and turns every device-side
    outcome into a terminal state (docs/CORE_API.md 5.2). Single use."""

    def __init__(
        self,
        session: DeviceSession,
        *,
        storage: Storage | None = None,
        timeout: float = CALIBRATION_TIMEOUT_S,
        expected_s: float = EXPECTED_CALIBRATION_S,
        progress_interval: float = 1.0,
    ) -> None:
        self.session = session
        self.state = CalibrationState.IDLE
        self.result: CalibrationResult | None = None
        self._storage = storage
        self._timeout = timeout
        self._expected_s = expected_s
        self._progress_interval = progress_interval
        self._running = False
        self._cancel_requested = False
        self._waiter: asyncio.Future[bool] | None = None
        self._progress_task: asyncio.Task | None = None
        self._done = asyncio.Event()
        self._started = False
        self._before: tuple[tuple[int, int], ...] | None = None
        self._after: tuple[tuple[int, int], ...] | None = None
        self._history_saved = False

    # -- state ------------------------------------------------------------------

    def _set_state(self, state: CalibrationState, detail: str = "") -> None:
        previous, self.state = self.state, state
        s = self.session
        self.session.bus.emit(
            CalibrationStateChanged(
                address=s.address, device_id=s.device_id, state=state, previous=previous, detail=detail
            )
        )

    def _finish(self, state: CalibrationState, *, error: str | None = None, detail: str = "") -> CalibrationResult:
        s = self.session
        result = CalibrationResult(
            address=s.address,
            device_id=s.device_id,
            state=state,
            started=self._started,
            before=self._before,
            after=self._after,
            error=error,
            detail=detail,
            history_saved=self._history_saved,
        )
        self._set_state(state, detail)
        self.result = result
        s.bus.emit(result)
        return result

    def _on_started(self) -> None:
        # tag52=4 was acked: learning has begun
        self._set_state(CalibrationState.LEARNING)
        self._progress_task = asyncio.ensure_future(self._progress(time.monotonic()))

    async def _progress(self, since: float) -> None:
        s = self.session
        while True:
            await asyncio.sleep(self._progress_interval)
            s.bus.emit(
                CalibrationProgress(
                    address=s.address,
                    device_id=s.device_id,
                    elapsed_s=time.monotonic() - since,
                    expected_s=self._expected_s,
                )
            )

    # -- run / cancel -----------------------------------------------------------------

    async def run(self) -> CalibrationResult:
        """Calibrate once. Device-side outcomes come back as the result's state;
        only a second call (RuntimeError) or a cancelled task (CancelledError,
        after cleanup) raise. The first call after a cancel() that came before
        it returns that CANCELLED result."""
        if self._running:
            raise RuntimeError("CalibrationJob.run() may only be called once")
        self._running = True
        if self.result is not None:  # cancel() came first, e.g. before a run() task started
            return self.result
        try:
            return await self._run()
        finally:
            self._done.set()

    async def _run(self) -> CalibrationResult:
        s = self.session
        if s.busy is not None:
            return self._finish(CalibrationState.FAILED, error=f"busy: {s.busy}")
        if s.state is not LinkState.CONNECTED:
            return self._finish(CalibrationState.LOST, detail=s._last_reason or "not connected")
        # the result is emitted inside the lock, which is released afterwards
        async with s.operation("calibration", suspend_keepalive=True) as ms:
            try:
                return await self._calibrate(ms)
            except asyncio.CancelledError:
                # cancelled outside the learning wait, which records its own result
                if self.result is None:
                    self._finish(CalibrationState.CANCELLED)
                raise
            finally:
                await self._stop_progress()

    async def _calibrate(self, ms: MS605) -> CalibrationResult:
        s = self.session
        try:
            cfg = await ms.read_config()
        except MS605ConnectionError as exc:
            return self._finish(CalibrationState.LOST, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - isolate every device-side failure
            return self._finish(CalibrationState.FAILED, error=str(exc))
        self._before = _pairs(cfg)
        if self._cancel_requested:
            return self._finish(CalibrationState.CANCELLED)
        self._set_state(CalibrationState.STARTING)
        self._started = True
        self._waiter = asyncio.ensure_future(
            ms.start_auto_calibration(
                timeout=self._timeout, keepalive_interval=s.keepalive_interval, on_started=self._on_started
            )
        )
        try:
            ok = await self._waiter
        except asyncio.CancelledError:
            # 5.3: the only predictable way to stop learning is to drop the link
            self._waiter.cancel()
            try:
                await asyncio.gather(self._waiter, return_exceptions=True)
                await self._stop_progress()
                await s.close()
            finally:
                self._finish(CalibrationState.CANCELLED, detail=_CANCEL_DETAIL)
            if self._cancel_requested:
                return self.result  # type: ignore[return-value]
            raise
        except Exception as exc:  # noqa: BLE001 - every device-side failure becomes a state
            await self._stop_progress()
            if isinstance(exc, MS605ConnectionError):
                return self._finish(CalibrationState.LOST, error=str(exc))
            if isinstance(exc, MS605TimeoutError):
                return self._finish(CalibrationState.TIMEOUT, error=str(exc))
            # incl. MS605DeviceError: the tag52 write was refused
            error = str(exc) if isinstance(exc, MS605Error) else f"{type(exc).__name__}: {exc}"
            return self._finish(CalibrationState.FAILED, error=error)
        await self._stop_progress()
        if not ok:
            return self._finish(CalibrationState.FAILED, detail="device reported failure")
        return await self._succeeded(ms)

    async def _succeeded(self, ms: MS605) -> CalibrationResult:
        s = self.session
        try:
            cfg = await ms.read_config()
        except asyncio.CancelledError:
            # learning already succeeded: report that, then honour the cancel
            self._finish(CalibrationState.SUCCEEDED, detail="반영값 재조회 실패: cancelled")
            raise
        except MS605Error as exc:
            return self._finish(CalibrationState.SUCCEEDED, detail=f"반영값 재조회 실패: {exc}")
        self._after = _pairs(cfg)
        detail = ""
        if self._storage is not None:
            try:
                self._storage.append_history(build_calibration_record(s.name, s.address, cfg, device_id=s.device_id))
                self._history_saved = True
            except StorageError as exc:
                detail = f"결과 저장 실패: {exc}"
        return self._finish(CalibrationState.SUCCEEDED, detail=detail)

    async def _stop_progress(self) -> None:
        task, self._progress_task = self._progress_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def cancel(self) -> None:
        """Stop the job. Before run(): CANCELLED with no device I/O. While
        STARTING/LEARNING: drops the link (5.3) and waits for run() to clean up;
        run() then returns the CANCELLED result."""
        if self.state in TERMINAL_CALIBRATION_STATES:
            return
        if not self._running:
            self._finish(CalibrationState.CANCELLED)
            return
        self._cancel_requested = True
        if self._waiter is not None:
            self._waiter.cancel()
        await self._done.wait()
