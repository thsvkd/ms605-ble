"""ms605.session -- one sensor's BLE link: state, keep-alive, operation lock,
live-output refcount, and push -> typed events. See docs/CORE_API.md section 4.

    session = DeviceSession(device, bus)
    await session.connect()
    async with session.operation("read") as ms:
        cfg = await ms.read_config()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from .driver import MS605, BLEDevice
from .errors import (
    FrameError,
    MS605ConnectionError,
    MS605DeviceError,
    MS605Error,
    MS605TimeoutError,
    SessionBusyError,
)
from .events import (
    BusyChanged,
    EventBus,
    FrameDropped,
    KeepAliveMissed,
    LinkState,
    LinkStateChanged,
    LiveRadar,
    PirChanged,
)
from .models import RadarOutputSnapshot, decode_pir_state, decode_radar_output, decode_supported_tags
from .protocol import (
    TAG_AMBIENT_LIGHT,
    TAG_BATTERY,
    TAG_DEVICE_ID,
    TAG_LIVE_RADAR_OUTPUT,
    TAG_PIR_STATE,
    TAG_VERSION,
    WRITE_TIMEOUT_S,
    ParsedFrame,
)

_log = logging.getLogger(__name__)

# Idle drop measured at 29.6 s; a 25 s interval held the link, 30 s did not.
KEEPALIVE_INTERVAL_S = 15.0
# The connectable window after a button press is >= 117 s (lower bound).
RECONNECT_TIMEOUT_S = 120.0
RELEASE_TIMEOUT_S = 5.0
_RECONNECT_PAUSE_S = 2.0  # pause between reconnect() attempts


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str  # tag30 lowercase hex
    battery_pct: int | None  # tag23 first byte
    version: tuple[int, ...] | None  # tag21, decode_supported_tags()
    light_lux: int | None  # tag36, big-endian integer


class DeviceSession:
    """Owns one MS605 driver for the life of a sensor's session. The same MS605
    object (and its push handler) is reused across reconnects."""

    def __init__(
        self,
        device: BLEDevice | str,
        bus: EventBus,
        *,
        scan: Callable[[float], Awaitable[list[BLEDevice]]] | None = None,
        client_factory: Callable[..., object] | None = None,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
        connect_timeout: float = 10.0,
        scan_secs: float = 5.0,
    ) -> None:
        self.bus = bus
        self.ms = MS605(device, client_factory=client_factory)
        self.address: str = getattr(device, "address", device)
        self.name: str | None = getattr(device, "name", None)
        self.state = LinkState.DISCONNECTED
        self.busy: str | None = None
        self.device_id: str | None = None
        self.info: DeviceInfo | None = None
        self.last_radar: RadarOutputSnapshot | None = None
        self.last_pir: bool | None = None
        self.keepalive_interval = keepalive_interval
        self._scan = scan or MS605.scan
        self._connect_timeout = connect_timeout
        self._scan_secs = scan_secs
        self._last_reason = ""  # why the link last went LOST; cleared on connect and close
        self._live = 0
        self._closes = 0  # bumped by close(): a connect that outlives a close() is undone
        # One connect attempt drives self.ms at a time: a connect started after a
        # close() waits until a late one has undone its own link, so that undo
        # never tears down the newer link on the shared driver.
        self._connect_lock = asyncio.Lock()
        self._suspended = False
        self._keepalive_task: asyncio.Task | None = None
        self._kick = asyncio.Event()  # set: ping now and restart the interval
        self.ms.add_push_handler(self._on_push)
        self.ms.on_disconnect = lambda: self._lose("BLE link lost")

    # -- state ----------------------------------------------------------------

    @property
    def last_lost_reason(self) -> str:
        """Why the link last went LOST; "" once it connects again or is closed."""
        return self._last_reason

    def _set_state(self, state: LinkState, reason: str = "") -> None:
        previous, self.state = self.state, state
        if previous is not state:
            self.bus.emit(
                LinkStateChanged(
                    address=self.address, device_id=self.device_id, state=state, previous=previous, reason=reason
                )
            )

    def _lose(self, reason: str) -> None:
        """CONNECTED -> LOST (unintended link loss). Sync: also called from the
        driver's on_disconnect callback."""
        if self.state is not LinkState.CONNECTED:
            return
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._last_reason = reason
        self._set_state(LinkState.LOST, reason)

    # -- connection -------------------------------------------------------------

    async def connect(self, device: BLEDevice | None = None) -> None:
        """Connect from DISCONNECTED or LOST, optionally retargeting a fresh
        handle (CoreBluetooth may hand out a new one after a button press)."""
        await self._establish(device, rescan=False)

    async def reconnect_once(self) -> None:
        """One attempt: rescan once, then reconnect via the advertised handle
        if this address is seen, else via the current one."""
        await self._establish(None, rescan=True)

    async def _establish(self, device: BLEDevice | None, *, rescan: bool) -> None:
        previous = self.state
        if previous in (LinkState.CONNECTING, LinkState.CONNECTED):
            raise MS605Error(f"cannot connect while {previous.value}")
        closes = self._closes
        # values seen on an earlier link are not current: the first push of this one counts as a change
        self.last_radar = self.last_pir = None
        self._set_state(LinkState.CONNECTING)
        try:
            if rescan:
                try:
                    found = await self._scan(self._scan_secs)
                except Exception as exc:  # noqa: BLE001 - fall back to the current handle
                    _log.warning("rescan before reconnecting %s failed: %s", self.address, exc)
                    found = []
                device = next((d for d in found if d.address.lower() == self.address.lower()), None)
            if device is not None:
                self.name = getattr(device, "name", None) or self.name
            async with self._connect_lock:
                if closes != self._closes:
                    raise MS605ConnectionError("session closed while connecting")
                try:
                    await self.ms.reconnect(device=device, timeout=self._connect_timeout)
                    if closes != self._closes:
                        raise MS605ConnectionError("session closed while connecting")
                    if not self.ms.is_connected:
                        raise MS605ConnectionError("BLE link lost while connecting")
                except BaseException:
                    if closes != self._closes:  # close() ran meanwhile: drop a link that landed late
                        await self._disconnect()
                    raise
        except BaseException as exc:
            if closes == self._closes:  # else close() already went DISCONNECTED
                self._set_state(previous, "" if isinstance(exc, asyncio.CancelledError) else str(exc))
            raise
        self._last_reason = ""
        self._set_state(LinkState.CONNECTED)
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        if self._live > 0 and self.busy != "calibration":
            try:
                await self.ms.set_live_output(True)
            except MS605Error as exc:
                _log.warning("re-enabling live output on %s failed: %s", self.address, exc)

    async def reconnect(self, *, timeout: float = RECONNECT_TIMEOUT_S) -> None:
        """Retry reconnect_once() every _RECONNECT_PAUSE_S until it succeeds
        or `timeout` passes (MS605TimeoutError). A dropped link only returns
        after a button press, so this waits for the sensor to advertise again.
        A close() meanwhile ends it with MS605ConnectionError: the app let go."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        closes = self._closes
        last: MS605ConnectionError | None = None
        while True:
            if closes != self._closes:  # checked before every attempt
                raise MS605ConnectionError("session closed") from last
            if self.state is LinkState.CONNECTED:  # e.g. the gather loop got there first
                return
            if self.state is not LinkState.CONNECTING:  # CONNECTING: another connect is in flight; wait
                try:
                    await self.reconnect_once()
                    return
                except MS605ConnectionError as exc:
                    last = exc
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MS605TimeoutError(f"{self.address} not reconnected within {timeout}s: {last}") from last
            await asyncio.sleep(min(_RECONNECT_PAUSE_S, remaining))

    async def close(self) -> None:
        """Stop keep-alive, disconnect within RELEASE_TIMEOUT_S (failures
        ignored) and go DISCONNECTED. Idempotent; allowed while the lock is held
        and while CONNECTING (that connect then ends in MS605ConnectionError)."""
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
        self._closes += 1  # also stops a reconnect() loop that is between attempts
        self._last_reason = ""
        try:
            if self.state is not LinkState.DISCONNECTED:
                self._set_state(LinkState.DISCONNECTED)
                await self._disconnect()
        finally:
            # awaited only now: a ping stuck mid-write ends once the link is dropped above
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def _disconnect(self) -> None:
        try:
            await asyncio.wait_for(self.ms.disconnect(), RELEASE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - releasing; nothing to salvage
            _log.warning("disconnecting %s failed: %s", self.address, exc)

    async def read_info(self) -> DeviceInfo:
        async with self.operation("identify") as ms:
            frame = await ms.read_raw([TAG_DEVICE_ID, TAG_BATTERY, TAG_VERSION, TAG_AMBIENT_LIGHT])
        raw_id = frame.get(TAG_DEVICE_ID)
        if not raw_id:
            raise MS605Error("device did not return tag 30 (device id)")
        battery = frame.get(TAG_BATTERY)
        version = frame.get(TAG_VERSION)
        light = frame.get(TAG_AMBIENT_LIGHT)
        self.info = DeviceInfo(
            device_id=raw_id.hex(),
            battery_pct=battery[0] if battery else None,
            version=decode_supported_tags(version) if version else None,
            light_lux=int.from_bytes(light, "big") if light else None,
        )
        self.device_id = self.info.device_id
        return self.info

    # -- operation lock -----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def operation(self, reason: str, *, suspend_keepalive: bool = False) -> AsyncIterator[MS605]:
        """The per-device lock around one batch of device I/O. Fails at once:
        SessionBusyError if held, MS605ConnectionError if not CONNECTED."""
        if self.busy is not None:
            raise SessionBusyError(self.busy)
        if self.state is not LinkState.CONNECTED:
            raise MS605ConnectionError("not connected")
        self.busy = reason
        self._suspended = suspend_keepalive
        live_before = self._live > 0
        if reason == "calibration" and not live_before:
            self.last_pir = None  # the device starts streaming on its own: a new stream
        self.bus.emit(BusyChanged(address=self.address, device_id=self.device_id, busy=reason))
        try:
            yield self.ms
        finally:
            self.busy = None
            self._suspended = False
            if suspend_keepalive:
                # ping at once: the gap since the caller's own last ping could
                # otherwise approach the 30 s idle drop
                self._kick.set()
            self.bus.emit(BusyChanged(address=self.address, device_id=self.device_id, busy=None))
            if reason == "calibration":
                await self._resync_live(live_before)

    async def _resync_live(self, was_on: bool) -> None:
        """acquire_live()/release_live() leave tag54 alone during calibration:
        once it ends, write tag54 if the count crossed 0 meanwhile."""
        want = self._live > 0
        if want is was_on or self.state is not LinkState.CONNECTED:
            return
        try:
            await self.ms.set_live_output(want)
        except MS605Error as exc:
            _log.warning("re-syncing live output on %s failed: %s", self.address, exc)

    # -- keep-alive -----------------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        self._kick.clear()
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), self.keepalive_interval)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            if self._suspended:
                continue
            try:
                # an ACK still missing when the next ping is due counts as missed; a slow
                # write still gets the driver's full stall deadline before the link is given up
                await self.ms.ping(timeout=min(WRITE_TIMEOUT_S, self.keepalive_interval), write_timeout=WRITE_TIMEOUT_S)
            except MS605DeviceError as exc:  # the device answered: the link is alive
                self._missed(exc, "error")
            except Exception as exc:  # noqa: BLE001 - classify, never crash the loop
                if isinstance(exc, MS605TimeoutError) and self.ms.is_connected:
                    self._missed(exc, "no_response")  # a lost or bad-CRC ACK; real loss is a ConnectionError
                    continue
                self._keepalive_task = None
                self._lose(str(exc))
                return

    def _missed(self, exc: Exception, kind: str) -> None:
        _log.warning("keep-alive on %s missed: %s", self.address, exc)
        self.bus.emit(KeepAliveMissed(address=self.address, device_id=self.device_id, error=str(exc), kind=kind))

    # -- live output -----------------------------------------------------------------

    async def acquire_live(self) -> None:
        """Count one viewer of live output (tag54). The first one turns it on.
        On a failed write the count stays raised: always release_live() in finally."""
        self._live += 1
        if self._live == 1 and self.state is LinkState.CONNECTED and self.busy != "calibration":
            self.last_pir = None  # a new stream: its first tag56 is reported even if unchanged
            await self.ms.set_live_output(True)

    async def release_live(self) -> None:
        if self._live == 0:
            return
        self._live -= 1
        if self._live == 0 and self.state is LinkState.CONNECTED and self.busy != "calibration":
            try:
                await self.ms.set_live_output(False)
            except MS605Error as exc:
                _log.warning("turning live output off on %s failed: %s", self.address, exc)

    # -- push -> events ------------------------------------------------------------------

    def _on_push(self, frame: ParsedFrame) -> None:
        for tag, value in frame.attributes:
            if tag == TAG_LIVE_RADAR_OUTPUT:
                try:
                    snapshot = decode_radar_output(value)
                except FrameError as exc:
                    self.bus.emit(
                        FrameDropped(address=self.address, device_id=self.device_id, tag=tag, reason=str(exc))
                    )
                    continue
                self.last_radar = snapshot
                self.bus.emit(LiveRadar(address=self.address, device_id=self.device_id, snapshot=snapshot))
            elif tag == TAG_PIR_STATE:
                detected = bool(decode_pir_state(value))
                if detected != self.last_pir:
                    self.last_pir = detected
                    self.bus.emit(PirChanged(address=self.address, device_id=self.device_id, detected=detected))
