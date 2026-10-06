"""ms605.gui.ws -- the hub: core events -> view messages with one server-wide
`seq`, per-client ordered queues, and the /ws connection loop
(docs/GUI_API.md section 7)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from pydantic import ValidationError
from starlette.websockets import WebSocket

from ms605.errors import StorageError
from ms605.events import (
    ApplyResult,
    BatchChanged,
    BusyChanged,
    CalibrationProgress,
    CalibrationResult,
    CalibrationStateChanged,
    Event,
    FrameDropped,
    GatherFailed,
    HandlerFailed,
    KeepAliveMissed,
    LinkState,
    LinkStateChanged,
    LiveRadar,
    PirChanged,
    SensorGathered,
)
from ms605.fleet import Fleet

from .apply import ApplyService
from .batch import BatchService
from .live import LiveFeed
from .schemas import (
    ApplyMessage,
    BatchMessage,
    CalibrationJobMessage,
    CalibrationSummary,
    ClientMessage,
    ConnectingDevice,
    GatherMessage,
    GatherStatus,
    LiveInfo,
    LiveSubscribeMessage,
    Notice,
    NoticeMessage,
    PendingData,
    PendingMessage,
    PendingView,
    RegistryInfo,
    SensorMessage,
    SensorRemoved,
    SensorRemovedMessage,
    SensorView,
    ServerInfo,
    SitesData,
    SitesMessage,
    SiteView,
    SnapshotMessage,
    SnapshotSummary,
    StateSnapshot,
)

_log = logging.getLogger(__name__)

SEND_TIMEOUT_S = 10.0
NOTICE_SUPPRESS_S = 10.0
CLOSE_TOO_SLOW = 1013
CLOSE_GOING_AWAY = 1001
MAX_CLIENT_FRAME = 4096  # bytes; a longer client frame is dropped (14.6.1)
_UNREAD = object()  # the history index has not been built

# Every ms605.events class (but the DeviceEvent base) is in exactly one of these;
# tests/test_gui_ws.py checks it, so a new core event fails there first.
HANDLED_EVENTS: frozenset[type[Event]] = frozenset(
    {
        LinkStateChanged,
        BusyChanged,
        SensorGathered,
        GatherFailed,
        HandlerFailed,
        KeepAliveMissed,
        FrameDropped,
        LiveRadar,
        PirChanged,
        CalibrationStateChanged,
        CalibrationProgress,
        CalibrationResult,
        BatchChanged,
        ApplyResult,
    }
)
IGNORED_EVENTS: frozenset[type[Event]] = frozenset()


class Client:
    """One /ws connection's ordered outbound queue. Overflow closes the
    connection (1013) instead of dropping a message (G5). Transient messages
    (`live`, `countdown`) go to latest-value slots instead (G14)."""

    def __init__(self, ws: WebSocket, maxsize: int) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize)
        self.slots: dict[str, str] = {}  # key -> latest serialized message; never counts toward the queue
        self._wake = asyncio.Event()  # set by put(), put_slot() and close()
        self.close_code: int | None = None

    def put(self, text: str) -> None:
        if self.close_code is not None:
            return
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.close(CLOSE_TOO_SLOW)
            return
        self._wake.set()

    def put_slot(self, key: str, text: str) -> None:
        if self.close_code is not None:
            return
        self.slots.pop(key, None)  # re-insert at the end: sensors take turns
        self.slots[key] = text
        self._wake.set()

    def drop_slot(self, key: str) -> None:
        self.slots.pop(key, None)

    def close(self, code: int) -> None:
        """Discard what is queued and have the sender close with `code`."""
        if self.close_code is not None:
            return
        self.close_code = code
        while not self.queue.empty():
            self.queue.get_nowait()
        self.slots.clear()
        self.queue.put_nowait(None)
        self._wake.set()

    async def run(self) -> None:
        """Send queued messages in order, then slot values, until closed or the peer is gone."""
        while True:
            if not self.queue.empty():
                text = self.queue.get_nowait()
                if text is None:
                    break
            elif self.slots:
                text = self.slots.pop(next(iter(self.slots)))
            else:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await asyncio.wait_for(self.ws.send_text(text), SEND_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.close_code = CLOSE_TOO_SLOW
                break
            except Exception:  # noqa: BLE001 - the peer went away; the receive side ends the connection
                return
        with contextlib.suppress(Exception):
            await self.ws.close(self.close_code)


class Hub:
    """Turns core events into view messages for every connected client."""

    def __init__(self, fleet: Fleet, server: ServerInfo, *, queue_size: int = 512, speed: float = 1.0) -> None:
        self.fleet = fleet
        self.registry = fleet.registry
        self.storage = fleet.storage
        self.server = server
        self.queue_size = queue_size
        self.seq = 0
        self.clients: set[Client] = set()
        self._connecting: dict[str, ConnectingDevice] = {}  # lowercase address -> not yet identified link
        self._gathered_at: dict[str, float] = {}
        self._notices: list[Notice] = []
        self._notice_last: dict[tuple[str, str | None], float] = {}
        self._dirty_sensors: set[str] = set()
        self._dirty_jobs: set[str] = set()
        self._dirty_sites = self._dirty_pending = self._dirty_gather = self._dirty_batch = False
        self._dirty_apply = False
        self._scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # last history record (line index, record) per device_id, and per lowercase address for lines
        # written before device ids existed; rebuilt only when the file's (mtime, size) changes
        self._history_key: object = _UNREAD
        self._history_by_id: dict[str, tuple[int, dict]] = {}
        self._history_by_address: dict[str, tuple[int, dict]] = {}
        self.live = LiveFeed(fleet, self)
        self.batches = BatchService(fleet, self, speed=speed)
        self.applies = ApplyService(fleet, self)

    # -- lifecycle -------------------------------------------------------------

    def attach(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubscribe = self.fleet.bus.subscribe(self.on_event)

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def close_clients(self, code: int = CLOSE_GOING_AWAY) -> None:
        for client in list(self.clients):
            client.close(code)

    # -- events -> dirty marks ---------------------------------------------------

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, (LiveRadar, PirChanged)):
            self.live.on_event(ev)  # throttled slot values, no seq
            return
        if isinstance(ev, LinkStateChanged):
            if ev.device_id is not None:
                self.mark_sensor(ev.device_id)
                self.live.on_event(ev)
            elif ev.state is LinkState.CONNECTING:
                self._connecting[ev.address.lower()] = ConnectingDevice(address=ev.address, since=ev.at)
                self.mark_gather()
            elif ev.state in (LinkState.DISCONNECTED, LinkState.LOST):
                self._forget_connecting(ev.address)
        elif isinstance(ev, BusyChanged):
            if ev.device_id is not None and ev.device_id in self.fleet.sessions:
                self.mark_sensor(ev.device_id)
        elif isinstance(ev, SensorGathered):
            if ev.device_id is not None:
                self._gathered_at[ev.device_id] = ev.at
                self.mark_sensor(ev.device_id)
            self._forget_connecting(ev.address)
            if ev.resolved_pending:
                self.mark_pending()
            self.live.on_event(ev)
        elif isinstance(ev, GatherFailed):
            self._forget_connecting(ev.address)
            self._notice(ev.at, "warning", "gather_failed", ev.error, ev.device_id, ev.address, ev.name)
        elif isinstance(ev, HandlerFailed):
            self._notice(ev.at, "error", "internal", f"{ev.event_type}: {ev.error}", None, None, None)
        elif isinstance(ev, (KeepAliveMissed, FrameDropped)):
            _log.debug("%s", ev)
        elif isinstance(ev, (BatchChanged, CalibrationStateChanged, CalibrationProgress, CalibrationResult)):
            self.batches.on_event(ev)
            if isinstance(ev, CalibrationResult):
                self.applies.on_event(ev)
        elif isinstance(ev, ApplyResult):
            self.applies.on_event(ev)
        else:
            return  # IGNORED_EVENTS
        self._schedule()

    def _forget_connecting(self, address: str) -> None:
        if self._connecting.pop(address.lower(), None) is not None:
            self.mark_gather()

    def _notice(self, at, level, code, message, device_id, address, name) -> None:
        key = (code, address)
        last = self._notice_last.get(key)
        if last is not None and at - last < NOTICE_SUPPRESS_S:
            return
        self._notice_last[key] = at
        self._notices.append(
            Notice(level=level, code=code, message=message, device_id=device_id, address=address, name=name, at=at)
        )

    def _schedule(self) -> None:
        if not self._scheduled and self._loop is not None:
            self._scheduled = True
            self._loop.call_soon(self.flush)

    def mark_sensor(self, device_id: str) -> None:
        self._dirty_sensors.add(device_id)

    def mark_sites(self) -> None:
        self._dirty_sites = True

    def mark_pending(self) -> None:
        self._dirty_pending = True

    def mark_gather(self) -> None:
        self._dirty_gather = True

    def mark_batch(self) -> None:
        self._dirty_batch = True
        self._schedule()

    def mark_apply(self) -> None:
        self._dirty_apply = True
        self._schedule()

    def mark_job(self, device_id: str) -> None:
        self._dirty_jobs.add(device_id)

    def clear_connecting(self) -> None:
        if self._connecting:
            self._connecting.clear()
            self.mark_gather()

    # -- dirty marks -> messages -------------------------------------------------

    def flush(self) -> None:
        """Publish everything marked, in the order sites, pending, sensors (by id), batch or
        calibration jobs (by id), apply, gather, notices."""
        self._scheduled = False
        if self._dirty_sites:
            self._dirty_sites = False
            self._publish(SitesMessage, SitesData(sites=self.sites()))
        if self._dirty_pending:
            self._dirty_pending = False
            self._publish(PendingMessage, PendingData(pending=self.pending()))
        dirty, self._dirty_sensors = sorted(self._dirty_sensors), set()
        for device_id in dirty:
            view = self.sensor_view(device_id)
            if view is None:
                self._publish(SensorRemovedMessage, SensorRemoved(device_id=device_id))
            else:
                self._publish(SensorMessage, view)
        jobs, self._dirty_jobs = sorted(self._dirty_jobs), set()
        if self._dirty_batch:  # the batch carries every job
            self._dirty_batch = False
            batch = self.batches.view()
            if batch is not None:
                self._publish(BatchMessage, batch)
        elif self.batches.current is not None:
            for device_id in jobs:
                self._publish(CalibrationJobMessage, self.batches.job_view(device_id))
        if self._dirty_apply:
            self._dirty_apply = False
            apply = self.applies.view()
            if apply is not None:
                self._publish(ApplyMessage, apply)
        if self._dirty_gather:
            self._dirty_gather = False
            self._publish(GatherMessage, self.gather_status())
        notices, self._notices = self._notices, []
        for notice in notices:
            self._publish(NoticeMessage, notice)

    def _publish(self, cls, data) -> None:
        self.seq += 1
        text = cls(seq=self.seq, ts=time.time(), data=data).model_dump_json()
        for client in list(self.clients):
            client.put(text)

    def put_slot_all(self, key: str, text: str) -> None:
        for client in list(self.clients):
            client.put_slot(key, text)

    def drop_slot_all(self, key: str) -> None:
        for client in list(self.clients):
            client.drop_slot(key)

    # -- clients -------------------------------------------------------------------

    def add_client(self, ws: WebSocket) -> Client:
        """Register a client with the snapshot as its first message. No await
        between the two: every later message lands in the queue exactly once."""
        client = Client(ws, self.queue_size)
        text = SnapshotMessage(seq=self.seq, ts=time.time(), data=self.snapshot()).model_dump_json()
        client.queue.put_nowait(text)
        self.clients.add(client)
        return client

    def remove_client(self, client: Client) -> None:
        self.clients.discard(client)

    async def serve(self, ws: WebSocket) -> None:
        """Run one accepted /ws connection until either side ends it."""
        client = self.add_client(ws)
        tasks = {asyncio.create_task(client.run()), asyncio.create_task(self._receive(ws, client))}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.remove_client(client)
            self.live.drop_client(client)
            for task in tasks:
                task.cancel()
            # wait, not gather: a gather cancelled mid-way raises a CancelledError of its own,
            # which an outer cancel scope (e.g. anyio's, in the TestClient) cannot recognise
            await asyncio.wait(tasks)

    async def _receive(self, ws: WebSocket, client: Client) -> None:
        """Hand each ClientMessage to the live feed until the peer disconnects. A frame that
        is binary, over MAX_CLIENT_FRAME bytes or not a ClientMessage is dropped (14.6.1)."""
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None or len(text.encode()) > MAX_CLIENT_FRAME:
                    _log.warning("dropped a binary or oversized websocket frame")
                    continue
                try:
                    parsed = ClientMessage.model_validate_json(text).root
                except ValidationError as exc:
                    _log.warning("dropped an invalid websocket message: %s", exc.errors()[:1])
                    continue
                if isinstance(parsed, LiveSubscribeMessage):
                    self.live.subscribe(client, parsed.data.device_ids)
                else:
                    self.live.unsubscribe(client, parsed.data.device_ids)
        except Exception as exc:  # noqa: BLE001 - the connection is gone either way
            _log.debug("websocket receive ended: %s", exc)

    # -- views (always rebuilt from the core's current state) -------------------------

    def sites(self) -> list[SiteView]:
        return [SiteView(site_id=s.site_id, name=s.name) for _, s in sorted(self.registry.sites.items())]

    def pending(self) -> list[PendingView]:
        return [
            PendingView(site_id=p.site_id, alias=p.alias, address=p.address, source=p.source)
            for p in self.registry.pending
        ]

    def gather_status(self) -> GatherStatus:
        connecting = sorted(self._connecting.values(), key=lambda c: c.since)
        return GatherStatus(gathering=self.fleet.gathering, connecting=connecting)

    def snapshot(self) -> StateSnapshot:
        ids = sorted(set(self.registry.sensors) | set(self.fleet.sessions))
        sensors = [v for v in (self.sensor_view(i) for i in ids) if v is not None]
        return StateSnapshot(
            seq=self.seq,
            server=self.server,
            gather=self.gather_status(),
            sites=self.sites(),
            sensors=sensors,
            pending=self.pending(),
            batch=self.batches.view(),
            apply=self.applies.view(),
        )

    def sensor_view(self, device_id: str) -> SensorView | None:
        sensor = self.registry.sensors.get(device_id)
        session = self.fleet.sessions.get(device_id)
        if sensor is None and session is None:
            return None
        registry = None
        addresses: list[str] = []
        if sensor is not None:
            registry = RegistryInfo(
                site_id=sensor.site_id,
                alias=sensor.alias,
                location=sensor.location,
                notes=sensor.notes,
                last_seen=sensor.last_seen,
                battery_pct=sensor.battery_pct,
            )
            addresses.extend(sensor.addresses.values())
        live = None
        if session is not None:
            info = session.info
            live = LiveInfo(
                address=session.address,
                mac=info.mac if info else None,
                name=session.name,
                link=session.state,
                auto_reconnect=self.fleet.recovery_enabled,
                busy=session.busy,
                lost_reason=session.last_lost_reason,
                battery_pct=info.battery_pct if info else None,
                firmware=".".join(map(str, info.version)) if info and info.version else None,
                light_lux=info.light_lux if info else None,
                gathered_at=self._gathered_at.setdefault(device_id, time.time()),
            )
            addresses.append(session.address)
        return SensorView(
            device_id=device_id,
            registry=registry,
            live=live,
            last_calibration=self._last_calibration(device_id, addresses),
            last_snapshot=self._last_snapshot(device_id),
            config_rev=self.applies.rev(device_id),
        )

    def _index_history(self) -> None:
        """Parse the shared history file once per change, not once per view (it only grows)."""
        try:
            st = self.storage.history_path.stat()
            key: object = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = None
        except OSError:
            key = _UNREAD  # let read_history() raise the StorageError
        if key == self._history_key and key is not _UNREAD:
            return
        records = self.storage.read_history()  # StorageError: keep the old index, try again next time
        by_id: dict[str, tuple[int, dict]] = {}
        by_address: dict[str, tuple[int, dict]] = {}
        for index, record in enumerate(records):  # the same matching as read_history(device_id, addresses=...)
            rec_id = record.get("device_id")
            if rec_id is None:
                address = record.get("device_address")
                if isinstance(address, str):
                    by_address[address.lower()] = (index, record)
            elif isinstance(rec_id, str):
                by_id[rec_id] = (index, record)
        self._history_by_id, self._history_by_address, self._history_key = by_id, by_address, key

    def _last_calibration(self, device_id: str, addresses: list[str]) -> CalibrationSummary | None:
        try:
            self._index_history()
        except StorageError as exc:
            _log.warning("reading calibration history for %s failed: %s", device_id, exc)
            return None
        found = [self._history_by_id.get(device_id)] + [self._history_by_address.get(a.lower()) for a in addresses]
        latest = max((f for f in found if f is not None), key=lambda f: f[0], default=None)
        if latest is None:
            return None
        last = latest[1]
        return CalibrationSummary(
            timestamp=str(last.get("timestamp", "")),
            sensitivity=last.get("sensitivity"),
            detect_mode=last.get("detect_mode"),
        )

    def _last_snapshot(self, device_id: str) -> SnapshotSummary | None:
        try:
            names = [p.stem for p in (self.storage.snapshots_dir / device_id).glob("*.json")]
            if not names:
                return None
            snap = self.storage.load_snapshot(device_id, max(names))
        except (StorageError, OSError) as exc:  # e.g. an unreadable snapshots directory
            _log.warning("reading the last snapshot of %s failed: %s", device_id, exc)
            return None
        return SnapshotSummary(name=snap.name, taken_at=snap.taken_at, reason=snap.reason)
