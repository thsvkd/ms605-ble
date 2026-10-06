"""ms605.driver -- async BLE control driver for the Meross MS605.

Usage::

    from ms605 import MS605

    devices = await MS605.scan()
    async with MS605(devices[0]) as ms605:
        config = await ms605.read_config()
        await ms605.set_sensitivity(3)

See ms605.protocol for the wire format and ms605.models for decoded value
types. Protocol coverage (implemented vs. intentionally out of scope) is
tracked in docs/SPEC.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice

    _BLEAK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only w/o bleak installed
    BleakClient = None  # type: ignore[assignment,misc]
    BleakScanner = None  # type: ignore[assignment,misc]
    BLEDevice = None  # type: ignore[assignment,misc]
    _BLEAK_IMPORT_ERROR = exc

from .discovery import friendly_ble_error, is_ms605_advertisement
from .errors import (
    MS605ConnectionError,
    MS605DeviceError,
    MS605Error,
    MS605TimeoutError,
)
from .models import (
    PROFILE_SECTION_KEYS,
    ConfigProfile,
    LightSample,
    MS605Config,
    PresenceHistoryRecord,
    SubSensorStatus,
    decode_config,
    decode_dnd,
    decode_light_history,
    decode_pir_state,
    decode_presence_history,
    decode_sample_intervals,
    decode_sub_sensor_status,
    encode_presence_absence_times,
    encode_sample_interval,
    encode_segment_map,
    encode_subsensor_enable,
    encode_zone_thresholds,
)
from .protocol import (
    CALIBRATION_TIMEOUT_S,
    DEFAULT_CHUNK_SIZE,
    INTER_CHUNK_DELAY_S,
    NOTIFY_CHAR_UUID,
    READ_CONFIG_TAGS,
    TAG_DETECT_MODE,
    TAG_DND,
    TAG_LIGHT_HISTORY_COUNT,
    TAG_LIGHT_HISTORY_PUSH,
    TAG_LIVE_OUTPUT_ENABLE,
    TAG_PIR_STATE,
    TAG_PRESENCE_ABSENCE_TIMES,
    TAG_PRESENCE_HISTORY_COUNT,
    TAG_PRESENCE_HISTORY_PUSH,
    TAG_READ_REQUEST,
    TAG_SAMPLE_INTERVAL,
    TAG_SEGMENT_MAP,
    TAG_SENSITIVITY,
    TAG_SPACE_LEARNING_RESULT,
    TAG_SUBSENSOR_ENABLE,
    TAG_SUBSENSOR_STATUS,
    TAG_TIME_SYNC,
    TAG_ZONE_ENABLE,
    TAG_ZONE_THRESHOLDS,
    WRITE_CHAR_UUID,
    WRITE_TIMEOUT_S,
    Attr,
    DetectMode,
    FrameReassembler,
    ParsedFrame,
    Sensitivity,
    advertised_mac,
    build_command,
    chunk_frame,
    msg_id_sequence,
    parse_frame,
)

_log = logging.getLogger(__name__)


def _settle(fut: asyncio.Future) -> None:
    """Cancel an unfinished future, or mark a finished one's exception as
    retrieved (so asyncio never warns about it)."""
    if not fut.done():
        fut.cancel()
    elif not fut.cancelled():
        fut.exception()


class MS605:
    """Async control driver for the Meross MS605 presence sensor's BLE
    configuration profile. See ms605.protocol for the wire format."""

    def __init__(
        self,
        address_or_device: str | BLEDevice,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        inter_chunk_delay: float = INTER_CHUNK_DELAY_S,
        client_factory: Callable[..., BleakClient] | None = None,
    ) -> None:
        if BleakClient is None:
            raise MS605Error(
                "bleak is not installed/importable; cannot construct a live MS605 "
                f"connection (import error: {_BLEAK_IMPORT_ERROR}). Offline frame "
                "building/parsing (ms605.protocol/ms605.models) does not need bleak "
                "and still works."
            )
        self._address_or_device = address_or_device
        self._mac = self.last_mac(address_or_device)
        self._chunk_size = chunk_size
        self._inter_chunk_delay = inter_chunk_delay
        # Seam for ms605.sim (and tests): builds the client instead of BleakClient.
        self._client_factory = client_factory or BleakClient
        self._client: BleakClient | None = None
        self._msg_ids: Iterator[int] = msg_id_sequence()
        self._reassembler = FrameReassembler()
        # Waiters resolve with (receive seq, frame): see _dispatch().
        self._pending: dict[int, asyncio.Future[tuple[int, ParsedFrame]]] = {}
        self._push_waiters: dict[int, list[asyncio.Future[tuple[int, ParsedFrame]]]] = {}
        # Count of frames dispatched so far: orders frames by arrival even when
        # several are dispatched before an awaiting task gets to run.
        self._rx_seq = 0
        self._push_handlers: list[Callable[[ParsedFrame], None]] = []
        # Called (no arguments) when the link drops on its own -- peer/idle
        # disconnect or a stalled write that abandons it, not
        # disconnect()/reconnect(). Pending requests have already been failed
        # with MS605ConnectionError by then.
        self.on_disconnect: Callable[[], None] | None = None
        # Serialises the chunked writes of concurrent _send() calls so a
        # background keep-alive ping can never interleave its chunks with an
        # operation's frame on the wire (both directions share one GATT
        # characteristic). Only the write is guarded; response waits are not.
        self._send_lock = asyncio.Lock()
        # The frame write in progress (under _send_lock), so _detach() can end
        # it: once the link is gone a half-written frame no longer matters.
        self._write_task: asyncio.Future[None] | None = None
        # Bumped by every _detach(): a request whose link went away while it
        # waited for _send_lock must never be written to the next link.
        self._link_gen = 0

    # -- discovery -----------------------------------------------------

    # Last advertised (RSSI, time.monotonic()) per address, recorded by
    # scan(): bleak's BLEDevice does not carry one (it lives in the
    # advertisement data). Readings older than _SCAN_RSSI_MAX_AGE_S are ignored.
    _scan_rssi: dict[str, tuple[int, float]] = {}
    _scan_mac: dict[str, str] = {}
    _SCAN_RSSI_MAX_AGE_S = 60.0

    @classmethod
    async def scan(cls, timeout: float = 5.0) -> list[BLEDevice]:
        """Scan for `timeout` seconds and return MS605 devices found, matched
        by service UUID, manufacturer-data signature, or name prefix. Each
        match's RSSI is available afterwards via last_rssi()."""
        if BleakScanner is None:
            raise MS605Error(f"bleak is not installed/importable ({_BLEAK_IMPORT_ERROR})")
        try:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except Exception as exc:  # pragma: no cover - requires real adapter
            raise MS605ConnectionError(friendly_ble_error(exc)) from exc

        matches: list[BLEDevice] = []
        for device, adv in discovered.values():
            if is_ms605_advertisement(device.name, adv.service_uuids, adv.manufacturer_data):
                matches.append(device)
                cls._scan_rssi[device.address] = (adv.rssi, time.monotonic())
                mac = advertised_mac(adv.manufacturer_data)
                if mac:
                    cls._scan_mac[device.address] = mac
                else:
                    cls._scan_mac.pop(device.address, None)
        return matches

    @classmethod
    def last_mac(cls, device: str | BLEDevice) -> str | None:
        """Manufacturer-advertised MAC from a recent scan, not the host UUID."""
        address = getattr(device, "address", device)
        return cls._scan_mac.get(address) if cls.last_rssi(address) is not None else None

    @property
    def mac(self) -> str | None:
        """Manufacturer MAC supplied by the browser bridge or native scan."""
        self._mac = getattr(self._client, "mac", None) or self.last_mac(self._address_or_device) or self._mac
        return self._mac

    @classmethod
    def last_rssi(cls, device: str | BLEDevice) -> int | None:
        """RSSI (dBm) of `device` (handle or address) from the most recent
        scan() that saw it, or None if no scan has within the last
        _SCAN_RSSI_MAX_AGE_S seconds."""
        entry = cls._scan_rssi.get(getattr(device, "address", device))
        if entry is None or time.monotonic() - entry[1] > cls._SCAN_RSSI_MAX_AGE_S:
            return None
        return entry[0]

    # -- connection lifecycle -------------------------------------------

    async def connect(self, timeout: float = 10.0) -> None:
        if self.is_connected:
            raise MS605Error("already connected; call disconnect() or reconnect() first")
        stale = self._detach("link replaced by connect()")
        if stale is not None:  # dead link whose disconnect callback never came
            with contextlib.suppress(Exception):
                await stale.disconnect()
        try:
            client = self._client_factory(
                self._address_or_device, timeout=timeout, disconnected_callback=self._on_disconnected
            )
            try:
                await client.connect()
                await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
            except BaseException:
                # The MS605 accepts one central at a time: a half-open link
                # left behind here would refuse every retry.
                with contextlib.suppress(Exception):
                    await client.disconnect()
                raise
        except Exception as exc:
            raise MS605ConnectionError(
                friendly_ble_error(exc, address=str(self._address_or_device))
            ) from exc
        self._client = client

    async def disconnect(self) -> None:
        client = self._detach("disconnected")
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                raise MS605ConnectionError(friendly_ble_error(exc)) from exc

    def _detach(self, reason: str) -> BleakClient | None:
        """Forget the current client and fail every in-flight request/push
        waiter with MS605ConnectionError(reason) -- they can never complete on
        this link. Returns the client for the caller to disconnect."""
        client, self._client = self._client, None
        self._link_gen += 1
        self._reassembler = FrameReassembler()
        write, self._write_task = self._write_task, None
        if write is not None:
            write.cancel()
        waiters = list(self._pending.values())
        for futs in self._push_waiters.values():
            waiters.extend(futs)
        self._pending.clear()
        self._push_waiters.clear()
        for fut in waiters:
            if not fut.done():
                fut.set_exception(MS605ConnectionError(reason))
        return client

    def _on_disconnected(self, client: object) -> None:
        if client is not self._client:
            return  # a link we already let go of (disconnect/reconnect)
        self._detach("BLE link lost")
        self._notify_link_lost()

    def _notify_link_lost(self) -> None:
        if self.on_disconnect is not None:
            try:
                self.on_disconnect()
            except Exception:  # noqa: BLE001 - never break the backend's callback
                _log.exception("on_disconnect hook failed")

    @property
    def is_connected(self) -> bool:
        """True while the underlying GATT link is live. The MS605 drops idle
        connections, so callers should re-check this before each operation and
        `reconnect()` if it went away."""
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.is_connected)
        except Exception:  # noqa: BLE001 - backend may throw once the link is gone
            return False

    async def reconnect(
        self,
        *,
        device: str | BLEDevice | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Re-establish the GATT link after an idle/peer disconnect.

        Registered push handlers survive; any in-flight request/push waiters
        fail with MS605ConnectionError (they can never complete on the dead
        link) and the reassembly buffer is reset so the new session starts
        clean. Pass `device` to retarget a freshly-rescanned handle
        (CoreBluetooth may hand out a new one)."""
        client = self._detach("link reset by reconnect()")
        if client is not None:
            with contextlib.suppress(Exception):  # stale link, nothing to salvage
                await client.disconnect()
        if device is not None:
            self._address_or_device = device
        await self.connect(timeout=timeout)

    async def __aenter__(self) -> MS605:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    # -- notification handling ------------------------------------------

    def _on_notify(self, _characteristic: object, data: bytearray) -> None:
        for raw in self._reassembler.feed(bytes(data)):
            try:
                parsed = parse_frame(raw)
            except Exception:  # noqa: BLE001 - drop malformed noise, never crash the callback
                continue
            if not parsed.crc_ok:  # SPEC §4.4: corrupted in transit, never trust its contents
                # frames carry ids/timestamps: keep the raw bytes out of shareable logs
                _log.warning(
                    "dropping frame with bad CRC (msgId=%d, %d bytes, crc 0x%04X != 0x%04X)",
                    parsed.msg_id, len(raw), parsed.actual_crc, parsed.expected_crc,
                )
                _log.debug("bad-CRC frame: %s", raw.hex())
                continue
            self._dispatch(parsed)

    def _dispatch(self, parsed: ParsedFrame) -> None:
        self._rx_seq += 1
        received = (self._rx_seq, parsed)
        if parsed.is_push:
            for tag, _ in parsed.attributes:
                waiters = self._push_waiters.pop(tag, None)
                if waiters:
                    for fut in waiters:
                        if not fut.done():
                            fut.set_result(received)
            # iterate a copy: a handler may remove itself mid-dispatch
            for handler in list(self._push_handlers):
                try:
                    handler(parsed)
                except Exception:  # noqa: BLE001 - one bad handler must not starve the rest
                    _log.exception("push handler %r failed", handler)
            return

        fut = self._pending.pop(parsed.msg_id, None)
        if fut is not None and not fut.done():
            fut.set_result(received)

    def add_push_handler(self, handler: Callable[[ParsedFrame], None]) -> None:
        """Register a callback invoked for every unsolicited push frame
        (tag 55 live radar output, tag 56 PIR, tag 58/60 history, tag 62
        space-learning result, tag 64 sub-sensor status, ...)."""
        self._push_handlers.append(handler)

    def remove_push_handler(self, handler: Callable[[ParsedFrame], None]) -> None:
        """Undo add_push_handler(). Safe to call even if `handler` was never
        registered or was already removed (no-op)."""
        try:
            self._push_handlers.remove(handler)
        except ValueError:
            pass

    # -- low-level send ---------------------------------------------------

    async def _write_chunks(self, frame: bytes) -> None:
        client = self._client
        if client is None:
            raise MS605ConnectionError("not connected; call connect() first")
        try:
            for chunk in chunk_frame(frame, self._chunk_size):
                await client.write_gatt_char(WRITE_CHAR_UUID, chunk, response=False)
                if self._inter_chunk_delay:
                    await asyncio.sleep(self._inter_chunk_delay)
        except Exception as exc:
            raise MS605ConnectionError(f"BLE write failed: {friendly_ble_error(exc)}") from exc

    async def _write_frame(self, frame: bytes, timeout: float, link_gen: int) -> None:
        """Write one frame's chunks under the send lock, atomically with
        respect to cancellation: a frame cut short would leave a partial frame
        in the device's reassembly buffer, so once started it is finished
        before a cancel is honoured -- unless the link is dropped meanwhile
        (the partial frame then no longer matters: MS605ConnectionError), or
        the write is still stuck after `timeout` seconds, which abandons the
        link (MS605TimeoutError). A pending cancel wins over either error.
        `link_gen` is the link the request was made on: if that link was reset
        while waiting for the lock, nothing is written (MS605ConnectionError) --
        its caller has already been told it failed."""
        async with self._send_lock:
            if self._link_gen != link_gen:
                raise MS605ConnectionError("BLE link reset before the request was sent")
            write = asyncio.ensure_future(self._write_chunks(frame))
            write.add_done_callback(_settle)
            self._write_task = write
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            cancelled = False
            try:
                while not write.done() and loop.time() < deadline:
                    try:
                        await asyncio.wait({write}, timeout=deadline - loop.time())
                    except asyncio.CancelledError:
                        cancelled = True
                stalled = not write.done()
                if stalled:
                    client = self._detach("BLE write stalled; link abandoned")  # also cancels `write`
                    if client is not None:
                        # its own disconnect callback will see a link we already let go of
                        self._notify_link_lost()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(client.disconnect(), timeout=5.0)
            finally:
                if self._write_task is write:
                    self._write_task = None
            if cancelled:
                raise asyncio.CancelledError
            if stalled:
                raise MS605TimeoutError(f"BLE write did not complete within {timeout}s; link abandoned")
            if write.cancelled():
                raise MS605ConnectionError("BLE link closed during a write")
            write.result()  # re-raises a write failure

    async def _send(
        self,
        attrs: Sequence[Attr],
        *,
        timeout: float = WRITE_TIMEOUT_S,
        expect_response: bool = True,
        write_timeout: float | None = None,
    ) -> ParsedFrame | None:
        received = await self._request(
            attrs, timeout=timeout, expect_response=expect_response, write_timeout=write_timeout
        )
        return None if received is None else received[1]

    async def _request(
        self,
        attrs: Sequence[Attr],
        *,
        timeout: float = WRITE_TIMEOUT_S,
        expect_response: bool = True,
        write_timeout: float | None = None,
    ) -> tuple[int, ParsedFrame] | None:
        """_send(), but the response comes with its receive seq (see _dispatch()).
        `timeout` bounds the response wait and, by default, the write too. With
        `write_timeout`, the write may take up to that long before the link is
        abandoned, so the exchange can last up to `write_timeout` plus a short
        ACK grace period (min(1 s, timeout / 4))."""
        msg_id = next(self._msg_ids)
        frame = build_command(attrs, msg_id)
        link_gen = self._link_gen
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout  # one budget for the write and the response wait
        fut: asyncio.Future[tuple[int, ParsedFrame]] | None = None
        if expect_response:
            fut = loop.create_future()
            self._pending[msg_id] = fut

        try:
            # guard only the write: two frames must not interleave chunks on the
            # wire, but their response waits (demuxed by msg_id) can overlap freely.
            await self._write_frame(frame, timeout if write_timeout is None else write_timeout, link_gen)
            if fut is None:
                return None
            # the frame is fully written: even if waiting for the send lock used
            # up the budget, give the response a short grace period to arrive
            floor = min(1.0, timeout / 4)
            try:
                seq, response = await asyncio.wait_for(fut, timeout=max(floor, deadline - loop.time()))
            except asyncio.TimeoutError as exc:
                raise MS605TimeoutError(f"no response to msgId={msg_id} within {timeout}s") from exc
        finally:
            if fut is not None:
                if self._pending.get(msg_id) is fut:
                    del self._pending[msg_id]
                _settle(fut)

        # defensive: any nonzero tag 3 fails the call (multi-write status layout unverified)
        status = response.first_error_status()
        if status is not None:
            raise MS605DeviceError(status, msg_id)
        return seq, response

    async def _read_via_push_or_response(self, tag: int, *, timeout: float) -> bytes:
        """Read `tag`, accepting either a value in the acknowledged response
        or a later unsolicited push carrying the same tag. `timeout` bounds
        the whole exchange, not each half."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        fut: asyncio.Future[tuple[int, ParsedFrame]] = loop.create_future()
        self._push_waiters.setdefault(tag, []).append(fut)
        try:
            response = await self.read_raw([tag], timeout=timeout)
            value = response.get(tag)
            if value is not None:
                return value
            try:
                _seq, push = await asyncio.wait_for(fut, timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError as exc:
                raise MS605TimeoutError(f"tag {tag} push not received within {timeout}s") from exc
            return push.get(tag) or b""
        finally:
            waiters = self._push_waiters.get(tag)
            if waiters and fut in waiters:
                waiters.remove(fut)
            _settle(fut)

    # -- high-level operations ---------------------------------------------

    async def read_config(
        self,
        tags: Sequence[int] = READ_CONFIG_TAGS,
        *,
        timeout: float = WRITE_TIMEOUT_S,
    ) -> MS605Config:
        """Multi-read `tags` (default: sensitivity/zones/mode/distances) and
        decode the response into an MS605Config."""
        attrs = [(TAG_READ_REQUEST, bytes([t])) for t in tags]
        response = await self._send(attrs, timeout=timeout)
        assert response is not None  # expect_response defaults True
        return decode_config(response)

    async def apply_profile(
        self,
        profile: ConfigProfile,
        sections: Sequence[str] | None = None,
        *,
        timeout: float = WRITE_TIMEOUT_S,
    ) -> list[str]:
        """Write the selected `sections` of a ConfigProfile onto this device --
        the write half of cloning one sensor's settings onto another (the read
        half is read_config() -> ConfigProfile.from_config()).

        `sections` restricts which parts are written (default: every populated
        section; a section whose profile value is None is always skipped).

        Two ordering/safety rules mirror what a careful operator would do by
        hand: sensitivity is written *before* zone thresholds, a harmless
        precaution -- on the 1-2 real devices measured, writing a preset level
        (tag 61) did not change the tag-51 values and a tag-51 write did not
        flip tag 61, so the two are independent as observed; and
        detect_mode == SPACE_LEARNING (4) is refused -- cloning it would
        *start a calibration* on the target rather than copy a setting (use
        start_auto_calibration() for that).

        A tag-51 write is acked at once but was not visible on an immediate
        read-back (visible ~1 s later), so callers that verify by reading back
        must poll rather than read once.

        Every selected section is validated first (ProfileError) so a bad
        section never leaves the target half-written; unselected ones are not
        checked.

        Returns the section keys actually written, in registry order."""
        wanted = set(PROFILE_SECTION_KEYS) if sections is None else set(sections)
        unknown = wanted - set(PROFILE_SECTION_KEYS)
        if unknown:
            raise ValueError(f"unknown profile section(s): {sorted(unknown)}")
        profile.validate(wanted)

        applied: list[str] = []
        if "sensitivity" in wanted and profile.sensitivity is not None:
            await self.set_sensitivity(profile.sensitivity, timeout=timeout)
            applied.append("sensitivity")
        if (
            "detect_mode" in wanted
            and profile.detect_mode is not None
            and int(profile.detect_mode) != int(DetectMode.SPACE_LEARNING)
        ):
            await self.set_detect_mode(profile.detect_mode, timeout=timeout)
            applied.append("detect_mode")
        if "zone_enable" in wanted and profile.zone_enable is not None:
            await self.set_zone_enable(profile.zone_enable, timeout=timeout)
            applied.append("zone_enable")
        if "zone_thresholds" in wanted and profile.zone_thresholds is not None:
            await self.set_zone_thresholds(profile.zone_thresholds, timeout=timeout)
            applied.append("zone_thresholds")
        if "subsensor_zones" in wanted and profile.subsensor_zones is not None:
            await self.set_subsensor_zones(profile.subsensor_zones, timeout=timeout)
            applied.append("subsensor_zones")
        if "subsensor_timing" in wanted and profile.subsensor_timing is not None:
            await self.set_subsensor_timing(profile.subsensor_timing, timeout=timeout)
            applied.append("subsensor_timing")
        if "subsensor_enable" in wanted and profile.subsensor_enable is not None:
            await self.set_subsensor_config(enabled=profile.subsensor_enable, timeout=timeout)
            applied.append("subsensor_enable")
        return applied

    async def read_raw(
        self, tags: Sequence[int], *, timeout: float = WRITE_TIMEOUT_S, write_timeout: float | None = None
    ) -> ParsedFrame:
        """Multi-read arbitrary `tags` and return the raw parsed response frame
        (use ParsedFrame.get(tag) to pull individual values). Useful for device
        metadata (tag 30 id, 36 ambient light, 21 version, 23 battery, ...).
        `write_timeout` (default: `timeout`) is as for ping()."""
        attrs = [(TAG_READ_REQUEST, bytes([t])) for t in tags]
        response = await self._send(attrs, timeout=timeout, write_timeout=write_timeout)
        assert response is not None
        return response

    async def set_sensitivity(self, level: int | Sensitivity, *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 61 (radar sensitivity): 1=LOW, 2=MEDIUM, 3=HIGH, 4=CUSTOM."""
        level = int(level)
        if level not in (1, 2, 3, 4):
            raise ValueError(f"sensitivity level must be 1-4 (LOW/MEDIUM/HIGH/CUSTOM), got {level}")
        await self._send([(TAG_SENSITIVITY, bytes([level]))], timeout=timeout)

    async def set_zone_thresholds(
        self,
        pairs: Sequence[tuple[int, int]],
        *,
        timeout: float = WRITE_TIMEOUT_S,
    ) -> None:
        """Write tag 51: exactly 7 (trigger, maintain) pairs, one per zone."""
        value = encode_zone_thresholds(pairs)
        await self._send([(TAG_ZONE_THRESHOLDS, value)], timeout=timeout)

    async def set_zone_enable(self, zones_enabled: Sequence[bool], *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 50: bit i = zone i enabled (up to 7/8 zones)."""
        if not 1 <= len(zones_enabled) <= 8:
            raise ValueError(f"expected 1-8 zone enable flags, got {len(zones_enabled)}")
        mask = sum(1 << i for i, e in enumerate(zones_enabled) if e)
        await self._send([(TAG_ZONE_ENABLE, bytes([mask]))], timeout=timeout)

    async def set_subsensor_config(
        self,
        *,
        enabled: Sequence[bool] | None = None,
        segment_map: bytes | None = None,
        presence_absence_times: Sequence[tuple[int, int]] | None = None,
        timeout: float = WRITE_TIMEOUT_S,
    ) -> None:
        """Write any combination of tags 41 (sub-sensor enable bitmask), 48
        (4-byte segment map, raw passthrough -- see set_subsensor_zones() for
        the friendlier per-zone form), and 49 (3 x [presence, absence]
        second pairs, one per sub-sensor) in a single frame."""
        if enabled is None and segment_map is None and presence_absence_times is None:
            raise ValueError("at least one of enabled/segment_map/presence_absence_times is required")
        attrs: list[Attr] = []
        if enabled is not None:
            attrs.append((TAG_SUBSENSOR_ENABLE, bytes([encode_subsensor_enable(enabled)])))
        if segment_map is not None:
            if len(segment_map) != 4:
                raise ValueError(f"segment_map must be exactly 4 bytes, got {len(segment_map)}")
            attrs.append((TAG_SEGMENT_MAP, bytes(segment_map)))
        if presence_absence_times is not None:
            attrs.append((TAG_PRESENCE_ABSENCE_TIMES, encode_presence_absence_times(presence_absence_times)))
        await self._send(attrs, timeout=timeout)

    async def set_subsensor_zones(
        self, zone_lists: Sequence[Sequence[int]], *, timeout: float = WRITE_TIMEOUT_S
    ) -> None:
        """Assign which zones (0..6) feed each of the 3 sub-sensors -- tag 48
        write. `zone_lists` is exactly 3 lists of zone indices, one per
        sub-sensor (Sensor1/2/3 in the app's own numbering)."""
        await self.set_subsensor_config(segment_map=encode_segment_map(zone_lists), timeout=timeout)

    async def set_subsensor_timing(
        self, timings: Sequence[tuple[int, int]], *, timeout: float = WRITE_TIMEOUT_S
    ) -> None:
        """Set each sub-sensor's (presence, absence) duration in seconds --
        tag 49 write. `timings` is exactly 3 (presence, absence) pairs, one
        per sub-sensor."""
        await self.set_subsensor_config(presence_absence_times=timings, timeout=timeout)

    async def set_detect_mode(self, mode: int | DetectMode, *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 52 (detect mode): 1=RADAR, 2=RADAR_WITH_PIR,
        3=PIR_WITH_RADAR, 4=SPACE_LEARNING (prefer start_auto_calibration()
        for #4, which also awaits the result)."""
        mode = int(mode)
        if mode not in (1, 2, 3, 4):
            raise ValueError(f"detect mode must be 1-4, got {mode}")
        await self._send([(TAG_DETECT_MODE, bytes([mode]))], timeout=timeout)

    async def set_live_output(self, enabled: bool, *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 54 (live-output enable): 1 makes the device stream tag-55
        live radar output pushes (per-zone current signal); 0 stops it."""
        await self._send(
            [(TAG_LIVE_OUTPUT_ENABLE, bytes([1 if enabled else 0]))], timeout=timeout
        )

    async def read_pir_state(self, *, timeout: float = WRITE_TIMEOUT_S) -> bool | None:
        """Explicit read of tag 56 (PIR state). Returns None if the device did
        not include tag 56 in its response."""
        frame = await self.read_raw([TAG_PIR_STATE], timeout=timeout)
        return decode_pir_state(frame.get(TAG_PIR_STATE))

    async def read_sub_sensor_status(
        self, *, sub_sensor_count: int = 3, timeout: float = WRITE_TIMEOUT_S
    ) -> tuple[SubSensorStatus, ...]:
        """Explicit read of tag 64 (sub-sensor status): per-sub-sensor
        presence flag plus presence/absence epoch timestamps."""
        frame = await self.read_raw([TAG_SUBSENSOR_STATUS], timeout=timeout)
        value = frame.get(TAG_SUBSENSOR_STATUS)
        if value is None:
            raise MS605Error("device did not return tag 64 (sub-sensor status) in its response")
        return decode_sub_sensor_status(value, sub_sensor_count=sub_sensor_count)

    async def set_dnd(self, enabled: bool, *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 32 (do-not-disturb toggle)."""
        await self._send([(TAG_DND, bytes([1 if enabled else 0]))], timeout=timeout)

    async def read_dnd(self, *, timeout: float = WRITE_TIMEOUT_S) -> bool:
        frame = await self.read_raw([TAG_DND], timeout=timeout)
        value = frame.get(TAG_DND)
        if value is None:
            raise MS605Error("device did not return tag 32 (DND) in its response")
        return decode_dnd(value)

    async def set_time(self, when: datetime | None = None, *, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Write tag 33 (time sync): u32 BE Unix epoch seconds. This driver
        does not call it automatically; call it explicitly when the device
        clock must be synchronized, such as before reading history timestamps."""
        epoch = int((when or datetime.now(timezone.utc)).timestamp())
        if not 0 <= epoch <= 0xFFFFFFFF:
            raise ValueError(f"epoch seconds out of u32 range: {epoch}")
        await self._send([(TAG_TIME_SYNC, struct.pack(">I", epoch))], timeout=timeout)

    async def set_sample_interval(
        self, sensor_index: int, seconds: int, *, timeout: float = WRITE_TIMEOUT_S
    ) -> None:
        """Write tag 98: per-sub-sensor sampling interval in seconds."""
        await self._send([(TAG_SAMPLE_INTERVAL, encode_sample_interval(sensor_index, seconds))], timeout=timeout)

    async def read_sample_interval(self, *, timeout: float = WRITE_TIMEOUT_S) -> dict[int, int]:
        frame = await self.read_raw([TAG_SAMPLE_INTERVAL], timeout=timeout)
        value = frame.get(TAG_SAMPLE_INTERVAL)
        if value is None:
            raise MS605Error("device did not return tag 98 (sample interval) in its response")
        return decode_sample_intervals(value)

    async def read_presence_history(
        self, *, detail: bool = False, timeout: float = WRITE_TIMEOUT_S
    ) -> list[PresenceHistoryRecord]:
        """Read presence-history records: a tag-59 count read, then tag-58
        record data (which may arrive as a trailing push -- see
        `_read_via_push_or_response`). EXPERIMENTAL: hardware behavior and
        pagination are not verified; this returns only one round trip."""
        count_frame = await self.read_raw([TAG_PRESENCE_HISTORY_COUNT], timeout=timeout)
        count_bytes = count_frame.get(TAG_PRESENCE_HISTORY_COUNT)
        count = int.from_bytes(count_bytes, "big") if count_bytes else 0
        if count == 0:
            return []
        raw = await self._read_via_push_or_response(TAG_PRESENCE_HISTORY_PUSH, timeout=timeout)
        return decode_presence_history(raw, detail=detail)

    async def read_light_history(self, *, timeout: float = WRITE_TIMEOUT_S) -> list[LightSample]:
        """Read light-history records: a tag-57 count read, then tag-60
        record data. It has the same experimental, single-round-trip caveat as
        read_presence_history()."""
        count_frame = await self.read_raw([TAG_LIGHT_HISTORY_COUNT], timeout=timeout)
        count_bytes = count_frame.get(TAG_LIGHT_HISTORY_COUNT)
        count = int.from_bytes(count_bytes, "big") if count_bytes else 0
        if count == 0:
            return []
        raw = await self._read_via_push_or_response(TAG_LIGHT_HISTORY_PUSH, timeout=timeout)
        return decode_light_history(raw)

    async def ping(self, *, timeout: float = WRITE_TIMEOUT_S, write_timeout: float | None = None) -> None:
        """Send a bare keep-alive command and await its ACK.

        The frame carries only the mandatory `tag 1 = 0x06` trailer appended by
        build_command(). Long operations use it to keep an otherwise idle GATT
        link active. It awaits the device's status ACK, so a failure also acts
        as a liveness signal. `write_timeout` (default: `timeout`) is how long a
        stalled write may take before the link is abandoned."""
        await self._send([], timeout=timeout, write_timeout=write_timeout)

    async def start_auto_calibration(
        self,
        *,
        timeout: float = CALIBRATION_TIMEOUT_S,
        keepalive_interval: float = 15.0,
        on_started: Callable[[], None] | None = None,
    ) -> bool:
        """Write tag52=4 (SPACE_LEARNING) then await the device's tag62 push
        with the result (value 1 == success). Learning can outlive the device's
        idle connection window.

        This method sends the bare keep-alive from ping() on a background task
        and races link loss against the result, so it returns promptly on
        success and fails fast on a lost link.

        Raises MS605ConnectionError if the link dies mid-learning (the link
        drops or a keep-alive write fails; a keep-alive answered with a
        non-zero status still proves the link is up and is only logged, as is
        an unanswered keep-alive while the link is still up) and
        MS605TimeoutError if no tag62 push arrives within
        `timeout` seconds. `keepalive_interval` is the seconds between pings and
        must stay below the device's idle-disconnect threshold. `on_started`
        is called once, after the tag52=4 ACK (learning has begun); an
        exception from it is only logged."""
        loop = asyncio.get_running_loop()
        result_fut: asyncio.Future[tuple[int, ParsedFrame]] = loop.create_future()
        self._push_waiters.setdefault(TAG_SPACE_LEARNING_RESULT, []).append(result_fut)
        link_lost: asyncio.Future[None] = loop.create_future()

        async def _keepalive() -> None:
            try:
                while True:
                    await asyncio.sleep(keepalive_interval)
                    try:
                        await self.ping()
                    except MS605DeviceError as exc:  # device answered: link is alive
                        _log.warning("keep-alive during auto-calibration got %s", exc)
                    except MS605TimeoutError as exc:
                        # a lost or bad-CRC ACK; real link loss is reported as
                        # MS605ConnectionError (or leaves the link marked down)
                        if not self.is_connected:
                            raise
                        _log.warning("keep-alive during auto-calibration not answered (link still up): %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure => link is gone
                if not link_lost.done():
                    link_lost.set_exception(
                        MS605ConnectionError(
                            f"BLE link lost during auto-calibration "
                            f"(keep-alive failed: {exc})"
                        )
                    )

        ka_task = asyncio.create_task(_keepalive())
        try:
            ack = await self._request([(TAG_DETECT_MODE, bytes([DetectMode.SPACE_LEARNING]))])
            assert ack is not None  # expect_response defaults True
            if (
                result_fut.done()
                and not result_fut.cancelled()
                and result_fut.exception() is None
                and result_fut.result()[0] < ack[0]
            ):
                # a tag62 received before our start's ACK belongs to an earlier,
                # aborted run -- keep waiting for this run's result. One received
                # after the ACK (even in the same notification) is this run's.
                _log.warning("ignoring a stale tag 62 result that preceded the calibration start")
                if not self.is_connected:  # detach already failed the old waiter; nothing would fail a new one
                    raise MS605ConnectionError("BLE link lost during auto-calibration")
                result_fut = loop.create_future()
                self._push_waiters.setdefault(TAG_SPACE_LEARNING_RESULT, []).append(result_fut)
            if on_started is not None:
                try:
                    on_started()
                except Exception:  # noqa: BLE001 - a caller's hook must not abort learning
                    _log.exception("on_started hook failed")
            done, _pending = await asyncio.wait(
                {result_fut, link_lost},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if result_fut in done:
                _seq, push = result_fut.result()
            elif link_lost in done:
                link_lost.result()  # re-raises the MS605ConnectionError
                raise AssertionError("unreachable")  # pragma: no cover
            else:
                raise MS605TimeoutError(
                    f"space-learning result (tag 62) not received within {timeout}s"
                )
        finally:
            ka_task.cancel()
            # absorbs ka_task's own CancelledError but not one aimed at this task
            await asyncio.gather(ka_task, return_exceptions=True)
            waiters = self._push_waiters.get(TAG_SPACE_LEARNING_RESULT)
            if waiters and result_fut in waiters:
                waiters.remove(result_fut)
            _settle(result_fut)
            # retrieve any link_lost exception we didn't consume so asyncio
            # doesn't warn about a never-retrieved future exception.
            if not link_lost.done():
                link_lost.cancel()
            elif not link_lost.cancelled():
                link_lost.exception()

        value = push.get(TAG_SPACE_LEARNING_RESULT)
        return bool(value) and value[0] == 1
