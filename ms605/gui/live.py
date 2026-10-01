"""ms605.gui.live -- the live monitor feed: per-connection subscriptions, one
session live reference per watched sensor, and `live` messages throttled per
sensor (docs/GUI_API.md 14.6.3, 14.6.4)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from ms605.errors import MS605Error
from ms605.events import Event, LinkStateChanged, LiveRadar, PirChanged, SensorGathered
from ms605.fleet import Fleet
from ms605.models import FALLBACK_DISTANCES_M, zone_distances
from ms605.session import DeviceSession

from .schemas import LiveData, LiveMessage, LiveZone

if TYPE_CHECKING:
    from .ws import Client, Hub

_log = logging.getLogger(__name__)

LIVE_INTERVAL_S = 0.25  # at most 4 Hz per sensor (G15)


def live_slot(device_id: str) -> str:
    return f"live:{device_id}"


class LiveFeed:
    """Who watches which sensor. While anyone watches a sensor that has a
    session, exactly one acquire_live() is held on that session (G12)."""

    def __init__(self, fleet: Fleet, hub: Hub) -> None:
        self.fleet = fleet
        self.hub = hub
        self.watchers: dict[str, set[Client]] = {}  # device_id -> connections that watch it
        self.held: dict[str, DeviceSession] = {}  # device_id -> the session whose live reference we hold
        self._tasks: dict[str, asyncio.Task] = {}  # one reconcile task per sensor
        self._again: set[str] = set()  # asked to reconcile again while its task runs
        self._radar_at: dict[str, float] = {}  # LiveRadar.at of the latest push
        self._last_emit: dict[str, float] = {}  # loop time
        self._handles: dict[str, asyncio.TimerHandle] = {}  # a scheduled emit

    # -- subscriptions (sync: called from the WS receive loop) ---------------------

    def subscribe(self, client: Client, device_ids: Sequence[str]) -> None:
        changed = []
        for device_id in dict.fromkeys(device_ids):
            clients = self.watchers.setdefault(device_id, set())
            if client not in clients:
                clients.add(client)
                changed.append(device_id)
        self._reconcile(changed)

    def unsubscribe(self, client: Client, device_ids: Sequence[str]) -> None:
        changed = []
        for device_id in dict.fromkeys(device_ids):
            client.drop_slot(live_slot(device_id))
            clients = self.watchers.get(device_id)
            if clients is None or client not in clients:
                continue
            clients.discard(client)
            if not clients:
                del self.watchers[device_id]
            changed.append(device_id)
        self._reconcile(changed)

    def drop_client(self, client: Client) -> None:
        self.unsubscribe(client, [i for i, clients in self.watchers.items() if client in clients])

    def refresh(self, device_ids: Iterable[str]) -> None:
        """Reconcile sensors whose session may have changed (e.g. after a release)."""
        self._reconcile([i for i in device_ids if i in self.watchers or i in self.held])

    def on_event(self, ev: Event) -> None:
        device_id = getattr(ev, "device_id", None)
        if device_id is None:
            return
        if isinstance(ev, (LiveRadar, PirChanged)):
            if isinstance(ev, LiveRadar):
                self._radar_at[device_id] = ev.at
            if self.watchers.get(device_id):
                self._throttle(device_id)
        elif isinstance(ev, (SensorGathered, LinkStateChanged)):
            if device_id in self.watchers or device_id in self.held:
                self._reconcile([device_id])

    # -- reconcile: one live reference per watched session ------------------------------

    def _reconcile(self, device_ids: Iterable[str]) -> None:
        for device_id in device_ids:
            if device_id in self._tasks:
                self._again.add(device_id)
            else:
                self._tasks[device_id] = asyncio.get_running_loop().create_task(self._reconcile_one(device_id))

    async def _reconcile_one(self, device_id: str) -> None:
        try:
            while True:
                self._again.discard(device_id)
                want = self.fleet.sessions.get(device_id) if self.watchers.get(device_id) else None
                held = self.held.get(device_id)
                if held is not want:
                    # anything raised is logged, never ends this task: a reconcile asked for meanwhile
                    # (`_again`) must still run, or `held` drifts from `watchers` (live output left on)
                    if held is not None:
                        del self.held[device_id]
                        try:
                            await held.release_live()  # the core only logs a failed tag54 write
                        except Exception:  # noqa: BLE001 - the count is already down
                            _log.exception("turning live output off for %s failed", device_id)
                    if want is not None:
                        self.held[device_id] = want
                        try:
                            await want.acquire_live()
                        except MS605Error as exc:  # the count stays raised; a reconnect rewrites tag54
                            _log.warning("turning live output on for %s failed: %s", device_id, exc)
                        except Exception:  # noqa: BLE001 - likewise: the count stays raised
                            _log.exception("turning live output on for %s failed", device_id)
                if device_id not in self._again:
                    return
        finally:
            self._tasks.pop(device_id, None)

    # -- throttled emit (G15) ------------------------------------------------------------

    def _throttle(self, device_id: str) -> None:
        if device_id in self._handles:
            return  # the scheduled emit will carry this value
        loop = asyncio.get_running_loop()
        last = self._last_emit.get(device_id)
        delay = 0.0 if last is None else max(0.0, last + LIVE_INTERVAL_S - loop.time())
        self._handles[device_id] = loop.call_later(delay, self._emit, device_id)

    def _emit(self, device_id: str) -> None:
        self._handles.pop(device_id, None)
        self._last_emit[device_id] = asyncio.get_running_loop().time()
        clients = self.watchers.get(device_id)
        session = self.fleet.sessions.get(device_id)
        if not clients or session is None or session.last_radar is None:
            return
        radar = session.last_radar
        dists = zone_distances(session.info) if session.info else FALLBACK_DISTANCES_M
        data = LiveData(
            device_id=device_id,
            at=self._radar_at.get(device_id, time.time()),
            pir=session.last_pir,
            sub_sensor_presence=list(radar.sub_sensor_presence),
            zones=[
                LiveZone(
                    index=z.index,
                    distance_m=dists[z.index],
                    enabled=z.enabled,
                    trigger_active=z.trigger_active,
                    trigger=z.current_trigger,
                    trigger_threshold=z.trigger_threshold,
                    maintain=z.current_maintain,
                    maintain_threshold=z.maintain_threshold,
                )
                for z in radar.zones
            ],
        )
        text = LiveMessage(ts=time.time(), data=data).model_dump_json()
        for client in list(clients):
            client.put_slot(live_slot(device_id), text)

    # -- shutdown -------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel scheduled emits and reconcile tasks. No device I/O: fleet.aclose()
        drops every link right after."""
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
