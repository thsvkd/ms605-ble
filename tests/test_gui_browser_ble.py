from __future__ import annotations

import asyncio
import json

import pytest
from bleak.exc import BleakError
from starlette.websockets import WebSocketDisconnect

from ms605.fleet import Fleet
from ms605.gui.browser_ble import BrowserBluetooth
from ms605.protocol import Sensitivity
from ms605.registry import Registry
from ms605.sim import SimMS605
from ms605.storage import Storage


class FakeWebSocket:
    def __init__(self, *, block_sends: bool = False) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict] = asyncio.Queue()
        self.closed: int | None = None
        self.send_gate = asyncio.Event()
        if not block_sends:
            self.send_gate.set()

    async def receive_text(self) -> str:
        text = await self.incoming.get()
        if text is None:
            raise WebSocketDisconnect(1000)
        return text

    async def send_text(self, text: str) -> None:
        await self.send_gate.wait()
        await self.outgoing.put(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        await self.incoming.put(None)

    def send(self, message: object) -> None:
        self.incoming.put_nowait(json.dumps(message))

    def send_raw(self, text: str) -> None:
        self.incoming.put_nowait(text)

    async def recv(self) -> dict:
        return await asyncio.wait_for(self.outgoing.get(), 1)


def test_hello_exposes_an_immediate_scan_peer_until_release() -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": "RFBL_A1B2C3"})
        serving = asyncio.create_task(radio.serve(ws))

        assert await ws.recv() == {"type": "ready"}
        devices = await radio.scan(999)
        assert len(devices) == 1
        assert devices[0].address.startswith("webbluetooth:")
        assert devices[0].name == "RFBL_A1B2C3"

        await radio.release([devices[0].address])
        await serving
        assert ws.closed == 1000
        assert await radio.scan(0) == []

    asyncio.run(run())


def test_hello_exposes_manufacturer_advertised_mac_on_client() -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        advertisement = bytes.fromhex("c01801001a0400010203190a1c00c4e7ae123456abcd00")
        ws.send({"type": "hello", "name": "RFBL_A1B2C3", "manufacturer_data": list(advertisement)})
        serving = asyncio.create_task(radio.serve(ws))

        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        client = radio.client_factory(device)
        assert client.mac == "C4:E7:AE:12:34:56"

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_peer_is_not_scannable_until_ready_has_been_sent() -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket(block_sends=True)
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        await asyncio.sleep(0)
        assert await radio.scan(0) == []

        ws.send_gate.set()
        assert await ws.recv() == {"type": "ready"}
        assert len(await radio.scan(0)) == 1
        await radio.aclose()
        await serving

    asyncio.run(run())


def test_missing_hello_is_bounded_and_closed() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(hello_timeout=0.01)
        ws = FakeWebSocket()
        await asyncio.wait_for(radio.serve(ws), 0.1)
        assert ws.closed == 1008
        assert await radio.scan(0) == []

    asyncio.run(run())


def test_adapter_rpc_notifications_and_disconnect_keep_the_peer() -> None:
    async def finish(ws: FakeWebSocket, task: asyncio.Task, expected: dict) -> None:
        request = await ws.recv()
        request_id = request.pop("id")
        assert request == {"type": "request", **expected}
        ws.send({"type": "result", "id": request_id})
        await task

    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=0.2)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)

        dropped: list[object] = []
        client = radio.client_factory(device, timeout=0.2, disconnected_callback=dropped.append)
        connecting = asyncio.create_task(client.connect())
        await finish(ws, connecting, {"op": "connect"})
        assert client.is_connected

        notified: list[tuple[object, bytes]] = []
        starting = asyncio.create_task(
            client.start_notify(
                "99e7be30-0003-4c6b-98a2-70fcb3471a72",
                lambda c, d: notified.append((c, bytes(d))),
            )
        )
        request = await ws.recv()
        request_id = request.pop("id")
        assert request == {"type": "request", "op": "start_notify"}
        ws.send({"type": "notify", "data": [0, 255, 17]})
        ws.send({"type": "result", "id": request_id})
        await starting
        assert notified == [("99e7be30-0003-4c6b-98a2-70fcb3471a72", b"\x00\xff\x11")]

        writing = asyncio.create_task(client.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"\x01\x80\xff"))
        await finish(ws, writing, {"op": "write", "data": [1, 128, 255]})

        disconnecting = asyncio.create_task(client.disconnect())
        await finish(ws, disconnecting, {"op": "disconnect"})
        assert not client.is_connected
        assert dropped == []
        assert [d.address for d in await radio.scan(0)] == [device.address]

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_websocket_drop_fails_pending_rpc_and_notifies_active_adapter() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=10)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": "MRBL_123ABC"})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        dropped: list[object] = []
        client = radio.client_factory(device, disconnected_callback=dropped.append)

        connecting = asyncio.create_task(client.connect())
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"]})
        await connecting

        writing = asyncio.create_task(
            client.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"pending")
        )
        assert (await ws.recv())["op"] == "write"
        await ws.incoming.put(None)
        with pytest.raises(BleakError, match="peer closed"):
            await asyncio.wait_for(writing, 0.1)
        await serving
        await asyncio.sleep(0)
        assert dropped == [client]
        assert not client.is_connected
        assert await radio.scan(0) == []

    asyncio.run(run())


def test_release_immediately_fails_pending_rpc_and_active_adapter() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=10)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        dropped: list[object] = []
        client = radio.client_factory(device, disconnected_callback=dropped.append)
        connecting = asyncio.create_task(client.connect())
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"]})
        await connecting
        writing = asyncio.create_task(client.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"pending"))
        assert (await ws.recv())["op"] == "write"

        await radio.release([device.address])
        with pytest.raises(BleakError, match="released"):
            await asyncio.wait_for(writing, 0.1)
        await asyncio.sleep(0)
        assert dropped == [client]
        assert await radio.scan(0) == []
        await serving

    asyncio.run(run())


@pytest.mark.parametrize(
    "hello",
    [
        "{",
        json.dumps({"type": "hello"}),
        json.dumps({"type": "hello", "name": 3}),
        json.dumps({"type": "hello", "name": "x" * 129}),
        json.dumps({"type": "hello", "name": None, "extra": True}),
        json.dumps({"type": "hello", "name": "x", "pad": "x" * 8192}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": "bad"}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": None}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": [256]}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": [-1]}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": [True]}),
        json.dumps({"type": "hello", "name": None, "manufacturer_data": [0] * 32}),
    ],
)
def test_invalid_hello_closes_with_policy_violation(hello: str) -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        ws.send_raw(hello)
        await asyncio.wait_for(radio.serve(ws), 0.1)
        assert ws.closed == 1008
        assert await radio.scan(0) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "message",
    [
        {"type": "result", "id": 0},
        {"type": "result", "id": True},
        {"type": "result", "id": 999},
        {"type": "result", "id": 1, "error": 7},
        {"type": "result", "id": 1, "error": "x" * 513},
        {"type": "notify", "data": [256]},
        {"type": "notify", "data": [-1]},
        {"type": "notify", "data": [True]},
        {"type": "notify", "data": [0] * 513},
        {"type": "disconnected", "extra": 1},
        {"type": "request", "id": 1, "op": "connect"},
    ],
)
def test_invalid_peer_message_closes_with_policy_violation(message: dict) -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        ws.send(message)
        await asyncio.wait_for(serving, 0.1)
        assert ws.closed == 1008
        assert await radio.scan(0) == []

    asyncio.run(run())


def test_gatt_disconnect_notifies_then_allows_stale_disconnect_and_reconnect() -> None:
    async def reply(ws: FakeWebSocket, task: asyncio.Task, op: str) -> None:
        request = await ws.recv()
        assert request["op"] == op
        ws.send({"type": "result", "id": request["id"]})
        await task

    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=0.2)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        dropped: list[object] = []
        stale = radio.client_factory(device, disconnected_callback=dropped.append)
        await reply(ws, asyncio.create_task(stale.connect()), "connect")

        ws.send({"type": "disconnected"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert dropped == [stale]
        assert not stale.is_connected
        assert len(await radio.scan(0)) == 1

        await reply(ws, asyncio.create_task(stale.disconnect()), "disconnect")
        current = radio.client_factory(device, disconnected_callback=dropped.append)
        await reply(ws, asyncio.create_task(current.connect()), "connect")
        assert current.is_connected
        assert ws.closed is None

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_late_result_after_gatt_disconnect_does_not_close_reconnectable_peer() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=0.2)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        client = radio.client_factory(device)
        connecting = asyncio.create_task(client.connect())
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"]})
        await connecting

        writing = asyncio.create_task(client.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"x"))
        request = await ws.recv()
        ws.send({"type": "disconnected"})
        ws.send({"type": "result", "id": request["id"], "error": "link lost"})
        with pytest.raises(BleakError, match="disconnected"):
            await writing
        await asyncio.sleep(0)
        assert ws.closed is None
        assert len(await radio.scan(0)) == 1

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_rpc_error_and_timeout_fail_the_connection() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=0.01)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)

        first_drops: list[object] = []
        first = radio.client_factory(device, disconnected_callback=first_drops.append)
        connecting = asyncio.create_task(first.connect())
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"]})
        await connecting
        writing = asyncio.create_task(first.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"x"))
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"], "error": "GATT write failed"})
        with pytest.raises(BleakError, match="GATT write failed"):
            await writing
        await asyncio.sleep(0)
        assert first_drops == [first]
        assert not first.is_connected

        second_drops: list[object] = []
        second = radio.client_factory(device, disconnected_callback=second_drops.append)
        connecting = asyncio.create_task(second.connect())
        request = await ws.recv()
        ws.send({"type": "result", "id": request["id"]})
        await connecting
        timing_out = asyncio.create_task(
            second.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"never answered")
        )
        assert (await ws.recv())["op"] == "write"
        with pytest.raises(TimeoutError):
            await timing_out
        await asyncio.sleep(0)
        assert second_drops == [second]
        assert not second.is_connected

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_rpc_timeout_includes_waiting_for_the_websocket_send_lock() -> None:
    async def run() -> None:
        radio = BrowserBluetooth(rpc_timeout=0.01)
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        ws.send_gate.clear()
        client = radio.client_factory(device, timeout=0.01)

        with pytest.raises(TimeoutError):
            await client.connect()
        assert not client.is_connected
        assert ws.outgoing.empty()

        ws.send_gate.set()
        await radio.aclose()
        await serving

    asyncio.run(run())


def test_adapter_rejects_wrong_characteristics_and_oversized_write_locally() -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        ws.send({"type": "hello", "name": None})
        serving = asyncio.create_task(radio.serve(ws))
        assert await ws.recv() == {"type": "ready"}
        (device,) = await radio.scan(0)
        client = radio.client_factory(device)

        with pytest.raises(BleakError, match="does not support notify"):
            await client.start_notify("wrong", lambda _c, _d: None)
        with pytest.raises(BleakError, match="not writable"):
            await client.write_gatt_char("wrong", b"x")
        with pytest.raises(BleakError, match="512"):
            await client.write_gatt_char("99e7be30-0002-4c6b-98a2-70fcb3471a72", b"x" * 513)
        assert ws.outgoing.empty()

        await radio.aclose()
        await serving

    asyncio.run(run())


def test_release_can_remove_one_of_multiple_peers_then_aclose_the_rest() -> None:
    async def run() -> None:
        radio = BrowserBluetooth()
        first, second = FakeWebSocket(), FakeWebSocket()
        first.send({"type": "hello", "name": "RFBL_000001"})
        second.send({"type": "hello", "name": "RFBL_000002"})
        tasks = [asyncio.create_task(radio.serve(first)), asyncio.create_task(radio.serve(second))]
        assert await first.recv() == {"type": "ready"}
        assert await second.recv() == {"type": "ready"}
        devices = await radio.scan(0)
        assert {device.name for device in devices} == {"RFBL_000001", "RFBL_000002"}

        first_address = next(device.address for device in devices if device.name == "RFBL_000001")
        await radio.release([first_address])
        assert first.closed == 1000
        assert second.closed is None
        assert [device.name for device in await radio.scan(0)] == ["RFBL_000002"]

        await radio.aclose()
        await asyncio.gather(*tasks)
        assert second.closed == 1000

    asyncio.run(run())


def test_fleet_uses_browser_peer_for_identity_config_and_settings(tmp_path, no_chunk_pacing) -> None:
    async def run_browser(ws: FakeWebSocket, device: SimMS605) -> None:
        client = None
        while True:
            message = await ws.recv()
            if message["type"] == "ready":
                continue
            request_id = message["id"]
            try:
                if message["op"] == "connect":
                    client = device.client_factory(
                        device.address,
                        disconnected_callback=lambda _client: ws.send({"type": "disconnected"}),
                    )
                    await client.connect()
                elif message["op"] == "start_notify":
                    assert client is not None
                    await client.start_notify(
                        "99e7be30-0003-4c6b-98a2-70fcb3471a72",
                        lambda _char, data: ws.send({"type": "notify", "data": list(data)}),
                    )
                elif message["op"] == "write":
                    assert client is not None
                    await client.write_gatt_char(
                        "99e7be30-0002-4c6b-98a2-70fcb3471a72",
                        bytes(message["data"]),
                        response=False,
                    )
                elif message["op"] == "disconnect":
                    assert client is not None
                    await client.disconnect()
                ws.send({"type": "result", "id": request_id})
            except Exception as exc:  # the browser serializes Web Bluetooth errors
                ws.send({"type": "result", "id": request_id, "error": str(exc)})

    async def run() -> None:
        radio = BrowserBluetooth()
        ws = FakeWebSocket()
        simulated = SimMS605(connectable_window=None, idle_timeout=None, speed=100)
        ws.send({"type": "hello", "name": simulated.name})
        serving = asyncio.create_task(radio.serve(ws))
        browser = asyncio.create_task(run_browser(ws, simulated))
        storage = Storage(root=tmp_path)
        fleet = Fleet(
            Registry(storage),
            storage,
            scan=radio.scan,
            client_factory=radio.client_factory,
            keepalive_interval=3600,
        )
        try:
            devices = await radio.scan(0)
            while not devices:
                await asyncio.sleep(0)
                devices = await radio.scan(0)
            (device,) = devices
            session = await fleet.connect(device)
            assert session.device_id == simulated.tags[30].hex()
            assert session.address.startswith("webbluetooth:")
            assert (await session.ms.read_config()).sensitivity == Sensitivity.MEDIUM
            await session.ms.set_sensitivity(Sensitivity.HIGH)
            assert (await session.ms.read_config()).sensitivity == Sensitivity.HIGH
            await session.acquire_live()

            async def wait_for_live() -> None:
                while session.last_radar is None:
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(wait_for_live(), 1)
            assert len(session.last_radar.zones) == 7
            await session.release_live()
        finally:
            await fleet.aclose()
            await radio.aclose()
            await serving
            browser.cancel()
            await asyncio.gather(browser, return_exceptions=True)

    asyncio.run(run())
