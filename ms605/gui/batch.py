"""ms605.gui.batch -- the server's one calibration batch (rounds of core
batches), its views, the countdown and the gather stop on fire
(docs/GUI_API.md 14.5.3-14.5.5, 14.6.5). Every check (404/409/422) is the
route's; this module only runs requests that passed them."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from ms605.calibration import EXPECTED_CALIBRATION_S
from ms605.events import (
    BatchChanged,
    BatchState,
    CalibrationProgress,
    CalibrationResult,
    CalibrationState,
    CalibrationStateChanged,
    Event,
)
from ms605.fleet import BatchCalibration, Fleet
from ms605.protocol import CALIBRATION_TIMEOUT_S

from .schemas import BatchView, CalibrationJobView, Countdown, CountdownMessage, StartMode, ZonePair

if TYPE_CHECKING:
    from .ws import Hub

_log = logging.getLogger(__name__)

COUNTDOWN_INTERVAL_S = 1.0
MAX_SCHEDULE_AHEAD_S = 86400.0
COUNTDOWN_SLOT = "countdown"

_ACTIVE = (BatchState.WAITING, BatchState.RUNNING)
_RETRYABLE = (CalibrationState.FAILED, CalibrationState.LOST, CalibrationState.TIMEOUT)
_RUNNING_JOB = (CalibrationState.STARTING, CalibrationState.LEARNING)
_CALIBRATION_EVENTS = (CalibrationStateChanged, CalibrationProgress, CalibrationResult)


def _pairs(pairs: tuple[tuple[int, int], ...] | None) -> list[ZonePair] | None:
    return None if pairs is None else [ZonePair(trigger=t, maintain=m) for t, m in pairs]


class BatchService:
    def __init__(self, fleet: Fleet, hub: Hub, *, speed: float = 1.0) -> None:
        self.fleet = fleet
        self.hub = hub
        self.expected_s = EXPECTED_CALIBRATION_S / speed  # progress denominator only (G21)
        self.timeout = CALIBRATION_TIMEOUT_S / speed
        self.current: BatchCalibration | None = None  # the current round's core batch
        self._batch_id = ""
        self._created_at = 0.0
        self._presence_override = False
        self._device_ids: list[str] = []  # selection order
        self._round = 0
        self._start: StartMode = "now"
        self._core_for: dict[str, BatchCalibration] = {}  # the core batch of each sensor's last attempt
        self._attempt: dict[str, int] = {}
        self._elapsed: dict[str, float] = {}  # from CalibrationProgress: the core keeps no elapsed time
        # the attempt each retried sensor had before the current round: back in place if the round never starts it
        self._previous: dict[str, tuple[BatchCalibration, int, float | None]] = {}
        self._countdown: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # gather stops on fire

    # -- queries --------------------------------------------------------------------

    def active(self) -> bool:
        return self.current is not None and self.current.state in _ACTIVE

    def members(self) -> frozenset[str]:
        core = self.current
        return frozenset(core.device_ids) if core is not None and core.state in _ACTIVE else frozenset()

    @property
    def batch_id(self) -> str | None:
        return self._batch_id if self.current is not None else None

    def view(self) -> BatchView | None:
        core = self.current
        if core is None:
            return None
        return BatchView(
            batch_id=self._batch_id,
            state=core.state,
            round=self._round,
            start=self._start,
            fire_at=core.fire_at,
            created_at=self._created_at,
            expected_s=self.expected_s,
            presence_override=self._presence_override,
            device_ids=list(self._device_ids),
            round_ids=list(core.device_ids),
            jobs=[self.job_view(i) for i in self._device_ids],
        )

    def job_view(self, device_id: str) -> CalibrationJobView:
        core = self._core_for[device_id]
        job = core.jobs.get(device_id)
        result = core.results.get(device_id) or (job.result if job else None)
        if result is not None:
            state, started = result.state, result.started
            error, detail = result.error, result.detail
            before, after, saved = _pairs(result.before), _pairs(result.after), result.history_saved
        elif job is not None:
            state, started = job.state, job.state in _RUNNING_JOB
            error, detail, before, after, saved = None, "", None, None, False
        else:
            state, started = CalibrationState.IDLE, False
            error, detail, before, after, saved = None, "", None, None, False
        return CalibrationJobView(
            batch_id=self._batch_id,
            device_id=device_id,
            attempt=self._attempt[device_id],
            state=state,
            started=started,
            elapsed_s=self._elapsed.get(device_id),
            error=error,
            detail=detail,
            before=before,
            after=after,
            history_saved=saved,
            retryable=state in _RETRYABLE,
        )

    def retryable_ids(self) -> list[str]:
        return [i for i in self._device_ids if self.job_view(i).retryable]

    # -- commands (checked by the route) ------------------------------------------------

    def create(
        self, ids: Sequence[str], mode: StartMode, start: float | datetime, *, presence_override: bool
    ) -> BatchView:
        core = self.fleet.calibrate(ids, start=start, timeout=self.timeout)  # ValueError: nothing changed
        self._batch_id = core.batch_id
        self._created_at = time.time()
        self._presence_override = presence_override
        self._device_ids = list(ids)
        self._round = 1
        self._core_for = dict.fromkeys(ids, core)
        self._attempt = dict.fromkeys(ids, 1)
        self._elapsed = {}
        self._previous = {}
        return self._begin_round(core, mode)

    def retry(self, ids: Sequence[str], mode: StartMode, start: float | datetime) -> BatchView:
        core = self.fleet.calibrate(ids, start=start, timeout=self.timeout)
        self._round += 1
        self._previous = {i: (self._core_for[i], self._attempt[i], self._elapsed.get(i)) for i in ids}
        for device_id in ids:
            self._core_for[device_id] = core
            self._attempt[device_id] += 1
            self._elapsed.pop(device_id, None)
        return self._begin_round(core, mode)

    def _begin_round(self, core: BatchCalibration, mode: StartMode) -> BatchView:
        self._stop_countdown()
        self.current = core
        self._start = mode
        if core.state is BatchState.WAITING and mode != "now":
            self._countdown = asyncio.get_running_loop().create_task(self._count_down(core))
        self.hub.mark_batch()
        view = self.view()
        assert view is not None
        return view

    async def cancel(self) -> BatchView | None:
        core = self.current
        if core is not None and core.state in _ACTIVE:
            await core.cancel()
        return self.view()

    # -- events ---------------------------------------------------------------------------

    def on_event(self, ev: Event) -> None:
        core = self.current
        if core is None:
            return
        if isinstance(ev, BatchChanged):
            if ev.batch_id != core.batch_id:
                return
            self.hub.mark_batch()
            if ev.state is not BatchState.WAITING:
                self._stop_countdown()
            if ev.state is BatchState.RUNNING and self.fleet.gathering:  # G20
                task = asyncio.get_running_loop().create_task(self._stop_gather())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        elif isinstance(ev, _CALIBRATION_EVENTS):
            if ev.device_id is None or ev.device_id not in core.device_ids:
                return
            if isinstance(ev, CalibrationProgress):
                self._elapsed[ev.device_id] = ev.elapsed_s
            if isinstance(ev, CalibrationResult) and ev.state is CalibrationState.CANCELLED and not ev.started:
                self._restore(ev.device_id)
            self.hub.mark_job(ev.device_id)

    def _restore(self, device_id: str) -> None:
        """A retry cancelled before it started this sensor (no tag52, e.g. cancelled in the
        countdown) did not happen for it: its earlier failed/lost result stays, retryable (14.5.5).
        Done on the event itself, so no flush ever shows the sensor `cancelled`."""
        previous = self._previous.pop(device_id, None)
        if previous is not None:
            self._core_for[device_id], self._attempt[device_id], elapsed = previous
            if elapsed is not None:
                self._elapsed[device_id] = elapsed

    async def _stop_gather(self) -> None:
        # finish_pending: a re-gather still identifying a sensor of this round must not be
        # cancelled (that closes its link); the job is waiting for its "identify" lock
        await self.fleet.stop_gather(finish_pending=True)
        self.hub.clear_connecting()
        self.hub.mark_gather()
        self.hub.flush()

    # -- countdown --------------------------------------------------------------------------

    async def _count_down(self, core: BatchCalibration) -> None:
        while core.state is BatchState.WAITING:
            now = time.time()
            data = Countdown(batch_id=self._batch_id, fire_at=core.fire_at, remaining_s=max(0.0, core.fire_at - now))
            self.hub.put_slot_all(COUNTDOWN_SLOT, CountdownMessage(ts=now, data=data).model_dump_json())
            await asyncio.sleep(COUNTDOWN_INTERVAL_S)

    def _stop_countdown(self) -> None:
        task, self._countdown = self._countdown, None
        if task is not None:
            task.cancel()
        self.hub.drop_slot_all(COUNTDOWN_SLOT)

    async def aclose(self) -> None:
        self._stop_countdown()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
