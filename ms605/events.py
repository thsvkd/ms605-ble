"""ms605.events -- typed core events, state enums, and the EventBus.

Every event is a frozen, keyword-only dataclass whose fields survive
`dataclasses.asdict()` + `json.dumps()`, so a server can forward one as
``{"type": type(ev).__name__, **asdict(ev)}``. See docs/CORE_API.md section 3.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .models import RadarOutputSnapshot

_log = logging.getLogger(__name__)


# -- state enums -------------------------------------------------------------


class LinkState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"


class CalibrationState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    LEARNING = "learning"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class BatchState(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class ApplyStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    FAILED = "failed"


TERMINAL_CALIBRATION_STATES: frozenset[CalibrationState] = frozenset(
    {
        CalibrationState.SUCCEEDED,
        CalibrationState.FAILED,
        CalibrationState.LOST,
        CalibrationState.TIMEOUT,
        CalibrationState.CANCELLED,
    }
)


# -- events -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Event:
    at: float = field(default_factory=time.time)  # epoch seconds


@dataclass(frozen=True, kw_only=True)
class DeviceEvent(Event):
    address: str  # this host's BLE address
    device_id: str | None  # tag30 lowercase hex; None until identified


@dataclass(frozen=True, kw_only=True)
class LinkStateChanged(DeviceEvent):
    state: LinkState
    previous: LinkState
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class KeepAliveMissed(DeviceEvent):
    error: str
    kind: str  # "error": the device answered with an error status; "no_response": no ACK, link still up


@dataclass(frozen=True, kw_only=True)
class BusyChanged(DeviceEvent):
    busy: str | None


@dataclass(frozen=True, kw_only=True)
class LiveRadar(DeviceEvent):
    snapshot: RadarOutputSnapshot


@dataclass(frozen=True, kw_only=True)
class PirChanged(DeviceEvent):
    detected: bool


@dataclass(frozen=True, kw_only=True)
class FrameDropped(DeviceEvent):
    tag: int
    reason: str


@dataclass(frozen=True, kw_only=True)
class SensorGathered(DeviceEvent):
    name: str | None
    known: bool
    site_id: str | None
    alias: str | None
    resolved_pending: bool


@dataclass(frozen=True, kw_only=True)
class GatherFailed(DeviceEvent):
    name: str | None
    error: str


@dataclass(frozen=True, kw_only=True)
class CalibrationStateChanged(DeviceEvent):
    state: CalibrationState
    previous: CalibrationState
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class CalibrationProgress(DeviceEvent):
    # host-side elapsed time since LEARNING began, not a device-reported progress (D6)
    elapsed_s: float
    expected_s: float


@dataclass(frozen=True, kw_only=True)
class CalibrationResult(DeviceEvent):
    state: CalibrationState
    started: bool  # whether tag52=4 was sent
    before: tuple[tuple[int, int], ...] | None
    after: tuple[tuple[int, int], ...] | None
    error: str | None
    detail: str = ""
    history_saved: bool = False


@dataclass(frozen=True, kw_only=True)
class BatchChanged(Event):
    batch_id: str
    state: BatchState
    fire_at: float
    device_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ApplyResult(DeviceEvent):
    reason: str
    status: ApplyStatus
    applied: tuple[str, ...]
    skipped: tuple[str, ...]
    mismatched: tuple[str, ...]
    snapshot: str | None
    error: str | None


@dataclass(frozen=True, kw_only=True)
class HandlerFailed(Event):
    event_type: str
    handler: str
    error: str


# -- bus ----------------------------------------------------------------------


class EventBus:
    """Synchronous fan-out of core events to callbacks and async streams.
    Single event loop only; not thread-safe."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Call `callback` (synchronously) for every emitted event. Returns a
        function that unsubscribes it; calling that again is a no-op."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def stream(self, *, maxsize: int = 256) -> EventStream:
        return EventStream(self, maxsize)

    def emit(self, event: Event) -> None:
        # iterate a copy: a callback may unsubscribe itself (or another) mid-dispatch
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - one bad subscriber must not starve the rest
                _log.exception("event handler %r failed on %s", callback, type(event).__name__)
                if isinstance(event, HandlerFailed):
                    continue  # never recurse on a failing HandlerFailed handler
                self.emit(
                    HandlerFailed(
                        event_type=type(event).__name__,
                        handler=getattr(callback, "__qualname__", repr(callback)),
                        error=str(exc),
                    )
                )


class EventStream:
    """Async iterator over a bus's events. A full queue drops its oldest
    event (counted in `dropped`) so a stalled consumer cannot grow memory."""

    def __init__(self, bus: EventBus, maxsize: int) -> None:
        self.dropped = 0
        self._maxsize = maxsize
        self._queue: deque[Event] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._unsubscribe = bus.subscribe(self._put)

    def _put(self, event: Event) -> None:
        if len(self._queue) >= self._maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(event)
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        self._queue.clear()
        self._ready.set()

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._queue:
                return self._queue.popleft()
            self._ready.clear()
            await self._ready.wait()

    async def __aenter__(self) -> EventStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()
