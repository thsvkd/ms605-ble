"""ms605.fleet -- many sensors at once: continuous gathering, batch
calibration, and drafts (validate -> snapshot -> write -> polled verify ->
rollback). Cloning is an absolute draft. See docs/CORE_API.md section 6.

    async with Fleet(Registry(storage), storage) as fleet:
        fleet.start_gather()
        ...
        await fleet.stop_gather()
        batch = fleet.calibrate(list(fleet.sessions), start=60.0)
        results = await batch.wait()
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from types import MappingProxyType

from .calibration import PREFLIGHT_WINDOW_S, CalibrationJob, PresenceSnapshot, presence_snapshot
from .driver import MS605, BLEDevice
from .errors import MS605Error, ProfileError, SessionBusyError, StorageError
from .events import (
    ApplyResult,
    ApplyStatus,
    BatchChanged,
    BatchState,
    CalibrationResult,
    CalibrationState,
    EventBus,
    GatherFailed,
    LinkState,
    SensorGathered,
)
from .models import PROFILE_SECTION_KEYS, PROFILE_ZONE_COUNT, ConfigProfile
from .protocol import CALIBRATION_TIMEOUT_S, DetectMode
from .registry import MatchResult, Registry
from .session import KEEPALIVE_INTERVAL_S, DeviceInfo, DeviceSession
from .storage import Storage

_log = logging.getLogger(__name__)

# A tag51 write reads back as the old value at once and as the new one ~1 s later.
VERIFY_TIMEOUT_S = 3.0
VERIFY_INTERVAL_S = 0.4
DND_SECTION = "dnd"  # a draft section outside ConfigProfile (tag32; docs/CORE_API.md 2.3)
GATHER_PAUSE_S = 1.0  # rest between gather scans, as in the old --collect loop
_FIRE_CHECK_S = 1.0  # a waiting batch re-reads the wall clock at least this often

_LIVE = (LinkState.CONNECTED, LinkState.CONNECTING)
_RETRY_STATES = (CalibrationState.FAILED, CalibrationState.LOST, CalibrationState.TIMEOUT)
_CONNECTIONS_FORMAT = "ms605-connections"


@dataclass(frozen=True)
class _RememberedConnection:
    address: str
    name: str | None
    mac: str | None


# -- gathering and connections ---------------------------------------------------


class Fleet:
    """The sessions of one working session (D5), keyed by device id."""

    def __init__(
        self,
        registry: Registry,
        storage: Storage,
        *,
        bus: EventBus | None = None,
        scan: Callable[[float], Awaitable[list[BLEDevice]]] | None = None,
        client_factory: Callable[..., object] | None = None,
        scan_secs: float = 5.0,
        connect_timeout: float = 10.0,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
        gather_pause: float = GATHER_PAUSE_S,
    ) -> None:
        self.bus = bus or EventBus()
        self.registry = registry
        self.storage = storage
        self._scan = scan or MS605.scan
        self._client_factory = client_factory
        self._scan_secs = scan_secs
        self._connect_timeout = connect_timeout
        self._keepalive_interval = keepalive_interval
        self._gather_pause = gather_pause
        self._sessions: dict[str, DeviceSession] = {}
        self._gather_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._recovery_interval = 1.0
        self._recovery_loaded = False
        self._connection_hosts: dict[str, dict[str, _RememberedConnection]] = {}
        self._scan_lock = asyncio.Lock()
        self._connecting: dict[str, asyncio.Task] = {}  # lowercase address -> gather connect task
        self._connecting_targets: dict[str, DeviceSession | None] = {}
        self._connecting_sources: dict[str, str] = {}
        self._unidentified: set[DeviceSession] = set()  # inside connect(), not yet in _sessions
        self._releasing: set[DeviceSession] = set()  # being closed by release(); never re-gathered
        self._batches: set[BatchCalibration] = set()  # not yet ended; aclose() cancels them

    @property
    def sessions(self) -> Mapping[str, DeviceSession]:
        return MappingProxyType(self._sessions)

    @property
    def gathering(self) -> bool:
        return self._gather_task is not None and not self._gather_task.done()

    @property
    def recovery_enabled(self) -> bool:
        return self._recovery_task is not None and not self._recovery_task.done()

    @property
    def connecting(self) -> int:
        """Connect attempts the gather loop has in flight."""
        return len(self._connecting)

    async def scan(self) -> list[BLEDevice]:
        async with self._scan_lock:
            return await self._scan(self._scan_secs)

    async def connect(self, device: BLEDevice) -> DeviceSession:
        """Connect, identify (tag30) and match against the registry. On any
        failure the new link is closed and the error raised."""
        session = self._new_session(device)
        self._unidentified.add(session)  # release() closes it, failing the connect
        try:
            await session.connect()
            info = await session.read_info()
            existing = self._sessions.get(info.device_id)
            if existing is not None and existing.state in _LIVE:
                raise MS605Error(f"duplicate device id {info.device_id} ({existing.address}, {session.address})")
            if existing in self._releasing:
                raise MS605Error(f"device {info.device_id} is being released")
            self._remember(session, info)
            # stored before any further await: release() owns it from here on
            self._sessions[info.device_id] = session
        except BaseException:
            await session.close()
            raise
        finally:
            self._unidentified.discard(session)
        match = self._match(session, info)
        if existing is not None:  # LOST/DISCONNECTED: the sensor came back under a new address
            await existing.close()
        self._emit_gathered(session, match)
        return session

    def _match(self, session: DeviceSession, info: DeviceInfo) -> MatchResult:
        try:
            return self.registry.match(info.device_id, session.address, battery_pct=info.battery_pct)
        except StorageError as exc:  # only a cache refresh failed: keep the link
            _log.warning("registry update for %s failed: %s", info.device_id, exc)
            return MatchResult(self.registry.sensors.get(info.device_id), False)

    def _emit_gathered(self, session: DeviceSession, match: MatchResult) -> None:
        sensor = match.sensor
        self.bus.emit(
            SensorGathered(
                address=session.address,
                device_id=session.device_id,
                name=session.name,
                known=sensor is not None,
                site_id=sensor.site_id if sensor else None,
                alias=sensor.alias if sensor else None,
                resolved_pending=match.resolved_pending,
            )
        )

    async def _regather(self, session: DeviceSession, device: BLEDevice) -> None:
        """Bring a LOST/DISCONNECTED session back on `device`, re-identify it as
        connect() does and emit SensorGathered. The link is closed only when another
        device id answers (or on cancellation): a failed read leaves it up, since a
        caller that reacted to CONNECTED may already hold the operation lock
        (SessionBusyError) and the identity was verified on the first gather."""
        device_id, info = session.device_id, session.info
        await session.connect(device)
        try:
            new = await session.read_info()
        except asyncio.CancelledError:
            await session.close()
            raise
        if new.device_id != device_id:  # another sensor at this address: keep the session's identity
            session.device_id, session.info = device_id, info
            await session.close()
            raise MS605Error(f"device id changed at {session.address}: expected {device_id}, got {new.device_id}")
        session.address = device.address
        try:
            self._remember(session, new)
        except StorageError:
            await session.close()
            raise
        self._emit_gathered(session, self._match(session, new))

    def _session_at(self, address: str) -> DeviceSession | None:
        return next((s for s in self._sessions.values() if s.address.lower() == address.lower()), None)

    def start_gather(self, *, accept: Callable[[BLEDevice], bool] | None = None) -> None:
        """Keep scanning and connect every advertising sensor (a LOST one
        comes back into its own session) until stop_gather()."""
        if not self.gathering:
            self._gather_task = asyncio.create_task(self._gather_loop(accept))

    async def _gather_loop(self, accept: Callable[[BLEDevice], bool] | None) -> None:
        while True:
            try:
                found = await self.scan()
            except Exception as exc:  # noqa: BLE001 - a failed scan is retried after the pause
                _log.warning("gather scan failed: %s", exc)
                found = []
            for device in found:
                key = device.address.lower()
                if (accept is not None and not accept(device)) or key in self._connecting:
                    continue
                if any(s.address.lower() == key for s in self._releasing):  # release() is closing it
                    continue
                session = self._session_at(device.address)
                if session is not None and session.state in _LIVE:
                    continue
                self._connecting[key] = asyncio.create_task(self._gather_one(device, session))
                self._connecting_targets[key] = session
                self._connecting_sources[key] = "gather"
            await asyncio.sleep(self._gather_pause)

    async def _gather_one(self, device: BLEDevice, session: DeviceSession | None) -> None:
        try:
            if session is not None:
                await self._regather(session, device)
            else:
                await self.connect(device)
        except Exception as exc:  # noqa: BLE001 - reported; the next scan retries
            self.bus.emit(
                GatherFailed(
                    address=device.address,
                    device_id=session.device_id if session else None,
                    name=getattr(device, "name", None),
                    error=str(exc),
                )
            )
        finally:
            key = device.address.lower()
            if self._connecting.get(key) is asyncio.current_task():
                del self._connecting[key]
                self._connecting_targets.pop(key, None)
                self._connecting_sources.pop(key, None)

    async def _cancel_connects(self, keys: Iterable[str]) -> None:
        selected = list(keys)
        tasks = [self._connecting.pop(k) for k in selected if k in self._connecting]
        for key in selected:
            self._connecting_targets.pop(key, None)
            self._connecting_sources.pop(key, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_gather(self, *, finish_pending: bool = False) -> None:
        """Stop scanning, then cancel in-flight connects -- or, with
        `finish_pending`, let them finish first. Connected sessions stay; no
        gather-started link appears after this returns."""
        task, self._gather_task = self._gather_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        keys = [key for key, source in self._connecting_sources.items() if source == "gather"]
        if finish_pending:
            await asyncio.gather(
                *(self._connecting[key] for key in keys if key in self._connecting),
                return_exceptions=True,
            )
        await self._cancel_connects(keys)

    # -- persistent automatic recovery -----------------------------------------

    @property
    def _connections_path(self):
        return self.storage.root / "connections.json"

    def _load_connections(self) -> None:
        if self._recovery_loaded:
            return
        path = self._connections_path
        data = self.storage.read_json(path)
        hosts: dict[str, dict[str, _RememberedConnection]] = {}
        if data is not None:
            if data.get("format") != _CONNECTIONS_FORMAT or data.get("version") != 1:
                raise StorageError(f"{path} is not a version-1 {_CONNECTIONS_FORMAT} file")
            raw_hosts = data.get("hosts")
            if not isinstance(raw_hosts, dict):
                raise StorageError(f"{path} is malformed: 'hosts' must be an object")
            try:
                for host, raw_connections in raw_hosts.items():
                    if not isinstance(host, str) or not host or not isinstance(raw_connections, dict):
                        raise ValueError("invalid host entry")
                    parsed: dict[str, _RememberedConnection] = {}
                    macs: set[str] = set()
                    for device_id, raw in raw_connections.items():
                        if not isinstance(device_id, str) or re.fullmatch(r"[0-9a-f]{2,128}", device_id) is None:
                            raise ValueError("invalid device id")
                        if not isinstance(raw, dict) or set(raw) != {"address", "name", "mac"}:
                            raise ValueError(f"invalid connection {device_id}")
                        address, name, mac = raw["address"], raw["name"], raw["mac"]
                        if not isinstance(address, str) or not address or len(address) > 512:
                            raise ValueError(f"invalid address for {device_id}")
                        if name is not None and (not isinstance(name, str) or len(name) > 128):
                            raise ValueError(f"invalid name for {device_id}")
                        if mac is not None:
                            if not isinstance(mac, str) or re.fullmatch(
                                r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac
                            ) is None:
                                raise ValueError(f"invalid MAC for {device_id}")
                            if mac.replace(":", "").lower() != device_id:
                                raise ValueError(f"MAC does not match device id {device_id}")
                        if mac is not None and mac in macs:
                            raise ValueError(f"duplicate connection for {device_id}")
                        if mac is not None:
                            macs.add(mac)
                        parsed[device_id] = _RememberedConnection(address, name, mac)
                    hosts[host] = parsed
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageError(f"{path} is malformed: {exc}") from exc

        migrated = self.registry.host not in hosts
        remembered = hosts.setdefault(self.registry.host, {})
        if migrated:
            for device_id, sensor in self.registry.sensors.items():
                address = sensor.addresses.get(self.registry.host)
                if address:
                    mac = (
                        ":".join(device_id[index:index + 2] for index in range(0, 12, 2)).upper()
                        if len(device_id) == 12
                        else None
                    )
                    remembered[device_id] = _RememberedConnection(address, None, mac)
        changed = migrated or data is None
        for device_id, session in self._sessions.items():
            if session.info is None:
                continue
            record = self._record(session, session.info)
            if remembered.get(device_id) != record:
                remembered[device_id] = record
                changed = True
        self._connection_hosts = hosts
        if changed:
            self._save_connections()
        for device_id, record in remembered.items():
            if device_id in self._sessions:
                continue
            session = self._new_session(record.address)
            session.name = record.name
            session.device_id = device_id
            session.info = DeviceInfo(device_id, None, None, None, mac=record.mac)
            self._sessions[device_id] = session
        self._recovery_loaded = True

    def _new_session(self, device: BLEDevice | str) -> DeviceSession:
        return DeviceSession(
            device,
            self.bus,
            scan=self._scan,
            client_factory=self._client_factory,
            keepalive_interval=self._keepalive_interval,
            connect_timeout=self._connect_timeout,
            scan_secs=self._scan_secs,
        )

    @staticmethod
    def _record(session: DeviceSession, info: DeviceInfo) -> _RememberedConnection:
        return _RememberedConnection(session.address, session.name, info.mac)

    def _save_connections(self) -> None:
        self.storage.write_json_atomic(
            self._connections_path,
            {
                "format": _CONNECTIONS_FORMAT,
                "version": 1,
                "hosts": {
                    host: {
                        device_id: {"address": item.address, "name": item.name, "mac": item.mac}
                        for device_id, item in connections.items()
                    }
                    for host, connections in self._connection_hosts.items()
                },
            },
        )

    def _remember(self, session: DeviceSession, info: DeviceInfo) -> None:
        if not self._recovery_loaded:
            return
        remembered = self._connection_hosts[self.registry.host]
        previous = remembered.get(info.device_id)
        record = self._record(session, info)
        if previous == record:
            return
        remembered[info.device_id] = record
        try:
            self._save_connections()
        except BaseException:
            if previous is None:
                del remembered[info.device_id]
            else:
                remembered[info.device_id] = previous
            raise

    def _forget(self, device_ids: Iterable[str]) -> None:
        if not self._recovery_loaded:
            return
        remembered = self._connection_hosts[self.registry.host]
        removed = {device_id: remembered[device_id] for device_id in device_ids if device_id in remembered}
        if not removed:
            return
        for device_id in removed:
            del remembered[device_id]
        try:
            self._save_connections()
        except BaseException:
            remembered.update(removed)
            raise

    def start_recovery(self, *, interval: float = 1.0) -> None:
        """Restore remembered sessions and keep reconnecting them until stopped."""
        if interval <= 0:
            raise ValueError("recovery interval must be positive")
        if self.recovery_enabled:
            return
        self._load_connections()
        self._recovery_interval = interval
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def stop_recovery(self) -> None:
        task, self._recovery_task = self._recovery_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        keys = [key for key, source in self._connecting_sources.items() if source == "recovery"]
        await self._cancel_connects(keys)

    def _recovery_session(self, device: BLEDevice) -> DeviceSession | None:
        remembered = self._connection_hosts.get(self.registry.host, {})
        address = device.address.lower()
        mac = MS605.last_mac(device)
        if mac is not None:
            for device_id, record in remembered.items():
                if record.mac == mac:
                    return self._sessions.get(device_id)
            # A verified, different MAC must never inherit another sensor's
            # recycled host address. Only legacy records without a MAC may use
            # the address fallback until an explicit gather replaces them.
            matches = [
                device_id
                for device_id, record in remembered.items()
                if record.mac is None and record.address.lower() == address
            ]
        else:
            matches = [
                device_id for device_id, record in remembered.items() if record.address.lower() == address
            ]
        return self._sessions.get(matches[0]) if len(matches) == 1 else None

    async def _recovery_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            candidates = [
                session
                for device_id, session in self._sessions.items()
                if device_id in self._connection_hosts[self.registry.host]
                and session.state not in _LIVE
                and session.busy is None
                and session not in self._releasing
            ]
            if not candidates or self.gathering:
                await asyncio.sleep(self._recovery_interval)
                continue
            try:
                async with self._scan_lock:
                    found = await self._scan(self._recovery_interval)
            except Exception as exc:  # noqa: BLE001 - automatic recovery keeps retrying
                _log.warning("recovery scan failed: %s", exc)
                found = []
            if self.gathering:
                continue
            for device in found:
                session = self._recovery_session(device)
                key = device.address.lower()
                if (
                    session is None
                    or session.state in _LIVE
                    or session.busy is not None
                    or session in self._releasing
                    or session in self._connecting_targets.values()
                    or key in self._connecting
                ):
                    continue
                self._connecting[key] = asyncio.create_task(self._gather_one(device, session))
                self._connecting_targets[key] = session
                self._connecting_sources[key] = "recovery"
            delay = self._recovery_interval - (loop.time() - started)
            if delay > 0:
                await asyncio.sleep(delay)

    async def release(self, device_ids: Iterable[str] | None = None) -> None:
        """Close these sessions (all if None, incl. ones still being connected)
        and forget them."""
        await self._release(device_ids, forget=True)

    async def _release(self, device_ids: Iterable[str] | None, *, forget: bool) -> None:
        ids = list(self._sessions) if device_ids is None else list(device_ids)
        sessions = [self._sessions[i] for i in ids]  # KeyError before anything is closed
        if forget:
            self._forget(ids)
        # marked before the first await: the gather loop must not reconnect a session
        # (or connect its address afresh) while it is being closed here
        marked = [s for s in sessions if s not in self._releasing]
        self._releasing.update(marked)
        try:
            keys = self._connecting if device_ids is None else [
                key for key, target in self._connecting_targets.items() if target in sessions
            ] + [s.address.lower() for s in sessions]
            await self._cancel_connects(keys)
            closing = sessions + (list(self._unidentified) if device_ids is None else [])
            await asyncio.gather(*(s.close() for s in closing))
            for device_id, session in zip(ids, sessions, strict=True):
                if self._sessions.get(device_id) is session:
                    del self._sessions[device_id]
        finally:
            self._releasing.difference_update(marked)

    async def aclose(self) -> None:
        """Cancel work and close links without forgetting automatic recovery."""
        try:
            await asyncio.gather(*(batch.cancel() for batch in list(self._batches)))
        finally:
            try:
                await self.stop_recovery()
            finally:
                try:
                    await self.stop_gather()
                finally:
                    await self._release(None, forget=False)

    async def __aenter__(self) -> Fleet:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- batch calibration ---------------------------------------------------------

    async def preflight(
        self, device_ids: Sequence[str], *, window_s: float = PREFLIGHT_WINDOW_S
    ) -> dict[str, PresenceSnapshot]:
        """Presence check on every device at once; a device's error becomes its
        snapshot's `error`."""
        sessions = [self._sessions[i] for i in device_ids]
        outcomes = await asyncio.gather(
            *(presence_snapshot(s, window_s=window_s) for s in sessions), return_exceptions=True
        )
        snapshots: dict[str, PresenceSnapshot] = {}
        for device_id, out in zip(device_ids, outcomes, strict=True):
            if isinstance(out, Exception):
                out = PresenceSnapshot(device_id, 0, None, None, None, error=str(out))
            elif isinstance(out, BaseException):
                raise out
            snapshots[device_id] = out
        return snapshots

    def calibrate(
        self, device_ids: Sequence[str], *, start: float | datetime = 0.0, timeout: float = CALIBRATION_TIMEOUT_S
    ) -> BatchCalibration:
        """Create a batch that fires `start` seconds from now, or at the
        datetime `start` (naive = local time), and return it at once."""
        ids = tuple(device_ids)
        if not ids:
            raise ValueError("no devices to calibrate")
        for device_id in ids:
            if device_id not in self._sessions:
                raise KeyError(device_id)
        now = time.time()
        fire_at = start.timestamp() if isinstance(start, datetime) else now + start
        if fire_at < now:
            raise ValueError(f"start time {start} is in the past")
        batch = BatchCalibration(self, ids, fire_at, timeout)
        self._batches.add(batch)
        batch._task.add_done_callback(lambda _: self._batches.discard(batch))
        return batch

    # -- drafts ------------------------------------------------------------------------

    async def apply(self, draft: Draft) -> dict[str, ApplyResult]:
        """Validate the whole draft (ProfileError before any I/O), then apply it
        to the targets one at a time, in order."""
        draft.validate()
        sessions = [self._sessions[t] for t in draft.targets]
        results: dict[str, ApplyResult] = {}
        for device_id, session in zip(draft.targets, sessions, strict=True):
            results[device_id] = await apply_changes(session, draft.changes_for(device_id), self.storage)
        return results

    async def rollback(self, device_id: str, snapshot: str) -> ApplyResult:
        """Re-apply a snapshot's sections. StorageError if it is missing or broken."""
        session = self._sessions[device_id]
        snap = self.storage.load_snapshot(device_id, snapshot)
        changes = SensorChanges.from_profile(snap.profile, snap.sections, dnd=snap.dnd)
        return await apply_changes(session, changes, self.storage, reason="rollback")


class BatchCalibration:
    """Calibrations of several devices fired together (docs/CORE_API.md 6.2).
    Made by Fleet.calibrate()."""

    def __init__(self, fleet: Fleet, device_ids: tuple[str, ...], fire_at: float, timeout: float) -> None:
        self.batch_id = uuid.uuid4().hex
        self.device_ids = device_ids
        self.fire_at = fire_at
        self.state = BatchState.WAITING
        self.jobs: dict[str, CalibrationJob] = {}
        self.results: dict[str, CalibrationResult] = {}
        self._fleet = fleet
        # the sessions as they are now: one released or replaced before the fire
        # counts as gone, so a later gather of the same sensor is never calibrated
        self._sessions = {i: fleet.sessions[i] for i in device_ids}
        self._timeout = timeout
        self._cancel = asyncio.Event()
        self._emit_state()
        self._task = asyncio.create_task(self._run())

    def _emit_state(self) -> None:
        self._fleet.bus.emit(
            BatchChanged(batch_id=self.batch_id, state=self.state, fire_at=self.fire_at, device_ids=self.device_ids)
        )

    def _set_state(self, state: BatchState) -> None:
        self.state = state
        self._emit_state()

    def _emit_result(
        self, device_id: str, state: CalibrationState, *, error: str | None = None, detail: str = ""
    ) -> CalibrationResult:
        result = CalibrationResult(
            address=self._sessions[device_id].address,
            device_id=device_id,
            state=state,
            started=False,
            before=None,
            after=None,
            error=error,
            detail=detail,
        )
        self._fleet.bus.emit(result)
        return result

    async def _fire_time_reached(self) -> bool:
        """Wait for fire_at, re-reading the wall clock at least every
        _FIRE_CHECK_S (robust to clock changes and sleep). False if cancelled."""
        while not self._cancel.is_set():
            remaining = self.fire_at - time.time()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(self._cancel.wait(), min(remaining, _FIRE_CHECK_S))
            except asyncio.TimeoutError:
                pass
        return False

    async def _run(self) -> None:
        if not await self._fire_time_reached():
            for device_id in self.device_ids:
                self.results[device_id] = self._emit_result(device_id, CalibrationState.CANCELLED)
            self._set_state(BatchState.CANCELLED)
            return
        for device_id in self.device_ids:
            session = self._fleet.sessions.get(device_id)
            if session is None or session is not self._sessions[device_id]:
                self.results[device_id] = self._emit_result(device_id, CalibrationState.LOST, detail="세션 없음")
            else:
                self.jobs[device_id] = CalibrationJob(session, storage=self._fleet.storage, timeout=self._timeout)
        self._set_state(BatchState.RUNNING)
        jobs = list(self.jobs.items())
        outcomes = await asyncio.gather(*(job.run() for _, job in jobs), return_exceptions=True)
        for (device_id, job), out in zip(jobs, outcomes, strict=True):
            if isinstance(out, CalibrationResult):
                self.results[device_id] = out
            elif job.result is not None:  # e.g. cancelled before its run() began
                self.results[device_id] = job.result
            else:
                _log.error("calibration of %s ended without a result: %r", device_id, out)
                self.results[device_id] = self._emit_result(device_id, CalibrationState.FAILED, error=repr(out))
        self._set_state(BatchState.CANCELLED if self._cancel.is_set() else BatchState.DONE)

    async def wait(self) -> dict[str, CalibrationResult]:
        """Wait for the batch to end; cancelling the waiter leaves the batch running."""
        await asyncio.wait({self._task})
        return dict(self.results)

    async def cancel(self) -> None:
        """WAITING: no device I/O, every device CANCELLED. RUNNING: cancel every
        job at once (each drops its link, docs/CORE_API.md 5.3)."""
        if self.state in (BatchState.DONE, BatchState.CANCELLED):
            return
        self._cancel.set()
        if self.state is BatchState.RUNNING:
            await asyncio.gather(*(job.cancel() for job in self.jobs.values()))
        await asyncio.wait({self._task})

    def retry_ids(self) -> list[str]:
        """Devices whose result is FAILED, LOST or TIMEOUT, in batch order."""
        return [i for i in self.device_ids if i in self.results and self.results[i].state in _RETRY_STATES]


# -- drafts -----------------------------------------------------------------------


@dataclass
class ThresholdChange:
    relative: bool  # True: current + value; False: overwrite with value
    trigger: list[int | None]  # 7 zones; None leaves that zone as it is
    maintain: list[int | None]


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _check_threshold_change(change: ThresholdChange) -> None:
    if not isinstance(change, ThresholdChange):
        raise ProfileError(f"zone_thresholds: expected a ThresholdChange, got {change!r}")
    for name in ("trigger", "maintain"):
        values = getattr(change, name)
        if not isinstance(values, (list, tuple)) or len(values) != PROFILE_ZONE_COUNT:
            raise ProfileError(f"zone_thresholds.{name}: expected {PROFILE_ZONE_COUNT} entries, got {values!r}")
        for zone, value in enumerate(values):
            if value is not None and not _is_int(value):
                raise ProfileError(f"zone_thresholds.{name}[{zone}]: expected an integer or None, got {value!r}")
            if value is not None and not change.relative and not 0 <= value <= 0xFFFF:
                raise ProfileError(f"zone_thresholds.{name}[{zone}]: {value} is outside 0..65535")


_PLAIN_SECTIONS = tuple(k for k in PROFILE_SECTION_KEYS if k != "zone_thresholds")


@dataclass
class SensorChanges:
    """The sections to change on one sensor. None means "leave as it is"."""

    sensitivity: int | None = None
    detect_mode: int | None = None
    zone_enable: list[bool] | None = None
    zone_thresholds: ThresholdChange | None = None
    subsensor_zones: list[list[int]] | None = None
    subsensor_timing: list[tuple[int, int]] | None = None
    subsensor_enable: list[bool] | None = None
    dnd: bool | None = None  # tag32, read and written only when set (not part of ConfigProfile)

    def _plain_profile(self) -> ConfigProfile:
        return ConfigProfile(**{k: copy.deepcopy(getattr(self, k)) for k in _PLAIN_SECTIONS})

    def validate(self) -> None:
        """Every check that needs no current values. ProfileError."""
        if self.dnd is not None and not isinstance(self.dnd, bool):
            raise ProfileError(f"dnd: expected true/false, got {self.dnd!r}")
        if self.zone_thresholds is not None:
            _check_threshold_change(self.zone_thresholds)
        self._plain_profile().validate()

    def resolve(self, current: ConfigProfile) -> ConfigProfile:
        """The target profile holding only the sections to write. Thresholds
        out of 0..65535 raise ProfileError (never clamped)."""
        self.validate()
        target = self._plain_profile()
        change = self.zone_thresholds
        if change is not None:
            if current.zone_thresholds is None:
                raise ProfileError("zone_thresholds: current values are unknown")
            pairs = []
            for zone, (cur_t, cur_m) in enumerate(current.zone_thresholds):
                pair = []
                for cur, value in ((cur_t, change.trigger[zone]), (cur_m, change.maintain[zone])):
                    new = cur if value is None else cur + value if change.relative else value
                    if not 0 <= new <= 0xFFFF:
                        raise ProfileError(f"zone_thresholds zone {zone}: {cur} + {value} = {new} is outside 0..65535")
                    pair.append(new)
                pairs.append((pair[0], pair[1]))
            target.zone_thresholds = pairs
        target.validate()
        if target.subsensor_timing is not None:  # pairs as tuples, like from_config(), so verify compares equal
            target.subsensor_timing = [(a, b) for a, b in target.subsensor_timing]
        return target

    @classmethod
    def from_profile(
        cls, profile: ConfigProfile, sections: Sequence[str], *, dnd: bool | None = None
    ) -> SensorChanges:
        """For clone and rollback: `sections` of `profile`, thresholds absolute.
        The "dnd" section takes its value from `dnd` (ConfigProfile has none)."""
        unknown = set(sections) - set(PROFILE_SECTION_KEYS + (DND_SECTION,))
        if unknown:
            raise ProfileError(f"unknown profile section(s): {sorted(unknown)}")
        changes = cls()
        for key in sections:
            if key == DND_SECTION:
                if dnd is None:
                    raise ProfileError("dnd: value unknown")
                changes.dnd = dnd
                continue
            value = copy.deepcopy(getattr(profile, key))
            if key == "zone_thresholds" and value is not None:
                value = ThresholdChange(relative=False, trigger=[t for t, _ in value], maintain=[m for _, m in value])
            setattr(changes, key, value)
        return changes


@dataclass
class Draft:
    targets: list[str]  # device ids, in apply order
    bulk: SensorChanges = field(default_factory=SensorChanges)
    per_sensor: dict[str, SensorChanges] = field(default_factory=dict)

    def changes_for(self, device_id: str) -> SensorChanges:
        """`bulk`, with every section set in `per_sensor[device_id]` replaced whole."""
        changes = copy.deepcopy(self.bulk)
        override = self.per_sensor.get(device_id)
        if override is not None:
            for f in fields(SensorChanges):
                value = getattr(override, f.name)
                if value is not None:
                    setattr(changes, f.name, copy.deepcopy(value))
        return changes

    def validate(self) -> None:
        if not self.targets:
            raise ProfileError("draft has no targets")
        self.bulk.validate()
        for changes in self.per_sensor.values():
            changes.validate()


# -- apply, verify, rollback -----------------------------------------------------------


async def poll_verify(
    ms: MS605,
    target: ConfigProfile,
    sections: Sequence[str],
    *,
    timeout: float = VERIFY_TIMEOUT_S,
    interval: float = VERIFY_INTERVAL_S,
    dnd: bool | None = None,
) -> tuple[ConfigProfile, list[str]]:
    """Re-read until `sections` match `target` (and DND equals `dnd`, when given)
    or `timeout` passes (the last read is at the deadline, never past it).
    Returns (profile read back, sections that still differ)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        actual = ConfigProfile.from_config(await ms.read_config())
        mismatched = target.diff_sections(actual, sections)
        if dnd is not None and await ms.read_dnd() != dnd:
            mismatched.append(DND_SECTION)
        remaining = deadline - loop.time()
        if not mismatched or remaining <= 0:
            return actual, mismatched
        await asyncio.sleep(min(interval, remaining))


async def apply_changes(
    session: DeviceSession,
    changes: SensorChanges,
    storage: Storage,
    *,
    reason: str = "apply",
    verify_timeout: float = VERIFY_TIMEOUT_S,
) -> ApplyResult:
    """One device: read -> resolve -> snapshot -> write -> polled verify, under
    the "apply" lock. The session must be identified (read_info()): snapshots
    are kept per device id. Device-side failures become the result's status;
    only CancelledError is raised. The result is also emitted on the session's bus."""
    out: dict = {"applied": (), "skipped": (), "mismatched": (), "snapshot": None, "error": None}
    try:
        if session.device_id is None:
            raise MS605Error("device not identified: call read_info() first")
        async with session.operation("apply") as ms:
            out["status"] = await _apply_locked(session, ms, changes, storage, reason, verify_timeout, out)
    except SessionBusyError as exc:
        out.update(status=ApplyStatus.FAILED, error=f"busy: {exc.reason}")
    except MS605Error as exc:  # not connected / not identified
        out.update(status=ApplyStatus.FAILED, error=str(exc))
    result = ApplyResult(address=session.address, device_id=session.device_id, reason=reason, **out)
    session.bus.emit(result)
    return result


async def _apply_locked(
    session: DeviceSession,
    ms: MS605,
    changes: SensorChanges,
    storage: Storage,
    reason: str,
    verify_timeout: float,
    out: dict,
) -> ApplyStatus:
    dnd = changes.dnd
    try:
        current = ConfigProfile.from_config(await ms.read_config())
        dnd_before = await ms.read_dnd() if dnd is not None else None
        target = changes.resolve(current)
        profile_sections = target.sections_present()
        sections = profile_sections + ((DND_SECTION,) if dnd is not None else ())
        learning = target.detect_mode is not None and int(target.detect_mode) == int(DetectMode.SPACE_LEARNING)
        written = tuple(s for s in sections if not (learning and s == "detect_mode"))  # apply_profile skips mode 4
        out["snapshot"] = storage.save_snapshot(session.device_id, current, written, reason, dnd=dnd_before).name
    except Exception as exc:  # noqa: BLE001 - nothing written yet
        out["error"] = str(exc)
        return ApplyStatus.FAILED
    try:
        applied = await ms.apply_profile(target, profile_sections)
        if dnd is not None:  # after the profile sections: a refused DND write does not block them
            await ms.set_dnd(dnd)
    except Exception as exc:  # noqa: BLE001 - what was written is unknown: re-read once (SPEC 6.2)
        out["error"] = str(exc) if isinstance(exc, MS605Error) else f"{type(exc).__name__}: {exc}"
        if ms.is_connected:
            try:
                actual = ConfigProfile.from_config(await ms.read_config())
                mismatched = target.diff_sections(actual, profile_sections)
                if dnd is not None and await ms.read_dnd() != dnd:
                    mismatched.append(DND_SECTION)
                out["mismatched"] = tuple(mismatched)
            except Exception as read_exc:  # noqa: BLE001 - leave mismatched empty
                _log.warning("re-read after a failed apply on %s failed: %s", session.address, read_exc)
        return ApplyStatus.FAILED
    out["applied"] = tuple(applied) + ((DND_SECTION,) if dnd is not None else ())
    out["skipped"] = tuple(s for s in sections if s not in out["applied"])
    try:
        _, mismatched = await poll_verify(ms, target, applied, timeout=verify_timeout, dnd=dnd)
    except Exception as exc:  # noqa: BLE001 - written but not confirmed
        out["error"] = str(exc)
        return ApplyStatus.UNVERIFIED
    out["mismatched"] = tuple(mismatched)
    return ApplyStatus.PARTIAL if mismatched else ApplyStatus.OK
