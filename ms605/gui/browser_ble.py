"""Bridge Web Bluetooth GATT operations into the driver's Bleak seams."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from starlette.websockets import WebSocket, WebSocketDisconnect

from ms605.protocol import ADV_COMPANY_ID, NOTIFY_CHAR_UUID, WRITE_CHAR_UUID, advertised_mac

MAX_FRAME_BYTES = 8192
MAX_NAME_CHARS = 128
MAX_DATA_BYTES = 512
MAX_MANUFACTURER_DATA_BYTES = 31
MAX_ERROR_CHARS = 512
CLOSE_POLICY_VIOLATION = 1008


class _ProtocolViolation(Exception):
    pass


def _message(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise _ProtocolViolation("message is not bounded text")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _ProtocolViolation("message is not JSON") from exc
    if not isinstance(value, dict):
        raise _ProtocolViolation("message is not an object")
    return value


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _byte_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_DATA_BYTES
        and all(type(item) is int and 0 <= item <= 255 for item in value)
    )


class BrowserBluetooth:
    """Browser Web Bluetooth peers presented as a small Bleak-compatible radio."""

    def __init__(self, *, rpc_timeout: float = 10.0, hello_timeout: float = 10.0) -> None:
        self.rpc_timeout = rpc_timeout
        self.hello_timeout = hello_timeout
        self._peers: dict[str, _Peer] = {}

    async def scan(self, timeout: float = 5.0) -> list[BLEDevice]:
        del timeout
        return [BLEDevice(peer.address, peer.name, None) for peer in self._peers.values()]

    def client_factory(
        self,
        address_or_device: object,
        *,
        timeout: float = 10.0,
        disconnected_callback: Callable[[object], None] | None = None,
        **_kwargs: object,
    ) -> _BrowserClient:
        address = str(getattr(address_or_device, "address", address_or_device))
        try:
            peer = self._peers[address]
        except KeyError:
            raise BleakError(f"no such browser Bluetooth peer: {address}") from None
        return _BrowserClient(
            peer,
            connect_timeout=timeout,
            rpc_timeout=self.rpc_timeout,
            disconnected_callback=disconnected_callback,
        )

    async def serve(self, ws: WebSocket) -> None:
        peer: _Peer | None = None
        try:
            hello = _message(await asyncio.wait_for(ws.receive_text(), self.hello_timeout))
            if set(hello) not in ({"type", "name"}, {"type", "name", "manufacturer_data"}) or hello["type"] != "hello":
                raise _ProtocolViolation("first message must be hello")
            name = hello["name"]
            if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME_CHARS):
                raise _ProtocolViolation("invalid browser Bluetooth name")
            manufacturer_data = hello.get("manufacturer_data")
            if "manufacturer_data" in hello and (
                not _byte_list(manufacturer_data) or len(manufacturer_data) > MAX_MANUFACTURER_DATA_BYTES
            ):
                raise _ProtocolViolation("invalid manufacturer data")
            mac = advertised_mac(
                {ADV_COMPANY_ID: bytes(manufacturer_data)} if "manufacturer_data" in hello else {}
            )
            address = f"webbluetooth:{uuid.uuid4()}"
            peer = _Peer(ws, address, name, mac)
            await peer.send({"type": "ready"})
            self._peers[address] = peer
            while True:
                await peer.receive(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        except (TimeoutError, _ProtocolViolation):
            await ws.close(CLOSE_POLICY_VIOLATION)
        finally:
            if peer is not None:
                if self._peers.get(peer.address) is peer:
                    del self._peers[peer.address]
                peer.close("browser Bluetooth peer closed")

    async def release(self, addresses: Iterable[str] | None = None) -> None:
        targets = list(self._peers) if addresses is None else list(addresses)
        peers: list[_Peer] = []
        for address in targets:
            peer = self._peers.pop(address, None)
            if peer is not None:
                peer.close("browser Bluetooth peer released")
                peers.append(peer)
        await asyncio.gather(*(peer.ws.close(1000) for peer in peers), return_exceptions=True)

    async def aclose(self) -> None:
        await self.release()


class _Peer:
    def __init__(self, ws: WebSocket, address: str, name: str | None, mac: str | None) -> None:
        self.ws = ws
        self.address = address
        self.name = name
        self.mac = mac
        self.send_lock = asyncio.Lock()
        self.pending: dict[int, asyncio.Future[None]] = {}
        self.retired: deque[int] = deque(maxlen=256)
        self.retired_set: set[int] = set()
        self.next_id = 1
        self.active: _BrowserClient | None = None
        self.closed = False

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message, separators=(",", ":")))

    async def rpc(self, op: str, *, data: bytes | None = None, timeout: float) -> None:
        if self.closed:
            raise BleakError("browser Bluetooth peer is closed")
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message: dict[str, Any] = {"type": "request", "id": request_id, "op": op}
        if data is not None:
            message["data"] = list(data)

        async def exchange() -> None:
            await self.send(message)
            await future

        try:
            await asyncio.wait_for(exchange(), timeout)
        except BaseException:
            if self.pending.pop(request_id, None) is not None:
                self._retire(request_id)
            raise

    def _retire(self, request_id: int) -> None:
        if request_id in self.retired_set:
            return
        if len(self.retired) == self.retired.maxlen:
            self.retired_set.discard(self.retired[0])
        self.retired.append(request_id)
        self.retired_set.add(request_id)

    async def receive(self, text: str) -> None:
        message = _message(text)
        message_type = message.get("type")
        if message_type == "result":
            if set(message) not in ({"type", "id"}, {"type", "id", "error"}):
                raise _ProtocolViolation("invalid result fields")
            request_id = message["id"]
            if not _positive_int(request_id):
                raise _ProtocolViolation("invalid result id")
            error = message.get("error")
            if "error" in message and (not isinstance(error, str) or len(error) > MAX_ERROR_CHARS):
                raise _ProtocolViolation("invalid result error")
            if request_id in self.retired_set:
                self.retired_set.remove(request_id)
                self.retired.remove(request_id)
                return
            if request_id not in self.pending:
                raise _ProtocolViolation("invalid result id")
            future = self.pending.pop(request_id)
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(BleakError(error))
        elif message_type == "notify":
            if set(message) != {"type", "data"} or not _byte_list(message["data"]):
                raise _ProtocolViolation("invalid notification")
            if self.active is not None:
                self.active._notify(message["data"])
        elif message_type == "disconnected":
            if set(message) != {"type"}:
                raise _ProtocolViolation("invalid disconnected event")
            self.link_lost("browser GATT disconnected")
        else:
            raise _ProtocolViolation("unknown browser Bluetooth message")

    def link_lost(self, reason: str) -> None:
        pending, self.pending = self.pending, {}
        for request_id, future in pending.items():
            self._retire(request_id)
            if not future.done():
                future.set_exception(BleakError(reason))
        if self.active is not None:
            active, self.active = self.active, None
            active._link_lost()

    def close(self, reason: str) -> None:
        self.closed = True
        self.link_lost(reason)


class _BrowserClient:
    def __init__(
        self,
        peer: _Peer,
        *,
        connect_timeout: float,
        rpc_timeout: float,
        disconnected_callback: Callable[[object], None] | None,
    ) -> None:
        self.address = peer.address
        self.mac = peer.mac
        self._peer = peer
        self._connect_timeout = connect_timeout
        self._rpc_timeout = rpc_timeout
        self._disconnected_callback = disconnected_callback
        self._notify_callback: Callable[[object, bytearray], None] | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._peer.closed

    async def _rpc(self, op: str, *, data: bytes | None = None, timeout: float | None = None) -> None:
        try:
            await self._peer.rpc(op, data=data, timeout=self._rpc_timeout if timeout is None else timeout)
        except BaseException:
            self._peer.link_lost(f"browser Bluetooth {op} failed")
            raise

    async def connect(self, **_kwargs: object) -> bool:
        self._peer.active = self
        await self._rpc("connect", timeout=self._connect_timeout)
        self._connected = True
        return True

    async def start_notify(
        self,
        char_specifier: object,
        callback: Callable[[object, bytearray], None],
        **_kwargs: object,
    ) -> None:
        if str(char_specifier).lower() != NOTIFY_CHAR_UUID:
            raise BleakError(f"characteristic {char_specifier} does not support notify")
        self._notify_callback = callback
        await self._rpc("start_notify")

    async def write_gatt_char(
        self,
        char_specifier: object,
        data: bytes,
        response: bool | None = None,
    ) -> None:
        del response
        if str(char_specifier).lower() != WRITE_CHAR_UUID:
            raise BleakError(f"characteristic {char_specifier} is not writable")
        raw = bytes(data)
        if len(raw) > MAX_DATA_BYTES:
            raise BleakError(f"GATT write exceeds {MAX_DATA_BYTES} bytes")
        await self._rpc("write", data=raw)

    async def disconnect(self) -> bool:
        await self._rpc("disconnect")
        self._connected = False
        if self._peer.active is self:
            self._peer.active = None
        return True

    def _notify(self, data: list[int]) -> None:
        if self._connected and self._notify_callback is not None:
            self._notify_callback(NOTIFY_CHAR_UUID, bytearray(data))

    def _link_lost(self) -> None:
        was_connected, self._connected = self._connected, False
        if was_connected and self._disconnected_callback is not None:
            asyncio.get_running_loop().call_soon(self._disconnected_callback, self)
