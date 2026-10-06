"""Remembered server BLE links survive web clients and GUI lifespans."""

import time

import pytest
from conftest import GUI_AUTH, GUI_BASE, GUI_TOKEN
from fastapi.testclient import TestClient

from ms605.fleet import Fleet
from ms605.gui.server import create_app
from ms605.registry import Registry
from ms605.sim import SimFleet
from ms605.storage import Storage


def _app(root, sim, *, transport="server"):
    storage = Storage(root=root)
    registry = Registry(storage, host="test-server")
    fleet = Fleet(
        registry, storage, scan=sim.discover, client_factory=sim.client_factory,
        scan_secs=0.01, gather_pause=0.01,
    )
    return create_app(fleet, registry, storage, GUI_TOKEN, sim=sim, ble_transport=transport)


def _state(client):
    response = client.get("/api/state", headers=GUI_AUTH)
    assert response.status_code == 200
    return response.json()


def _connected(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sensors = _state(client)["sensors"]
        if sensors and sensors[0]["live"]["link"] == "connected":
            return sensors[0]
        time.sleep(0.01)
    raise AssertionError("remembered sensor did not reconnect")


@pytest.mark.usefixtures("no_chunk_pacing")
def test_server_remembers_unnamed_sensor_across_tab_and_server_restart(tmp_path):
    sim = SimFleet(1, speed=1)
    app = _app(tmp_path, sim)
    with TestClient(app, base_url=GUI_BASE) as client:
        client.post("/api/sim/press/1", headers=GUI_AUTH)
        client.post("/api/gather/start", headers=GUI_AUTH)
        first = _connected(client)
        assert first["registry"] is None
        assert first["live"]["auto_reconnect"] is True
        client.post("/api/gather/stop", headers=GUI_AUTH)
        # Closing a web socket must not close a server-owned BLE session.
        for _ in range(2):
            with client.websocket_connect("/ws", headers={**GUI_AUTH, "Host": "127.0.0.1:8605"}) as ws:
                sensor = ws.receive_json()["data"]["sensors"][0]
                assert sensor["device_id"] == first["device_id"]
                assert sensor["live"]["link"] == "connected"
        assert _connected(client)["device_id"] == first["device_id"]

    restored = _app(tmp_path, sim)
    with TestClient(restored, base_url=GUI_BASE) as client:
        state = _state(client)
        assert state["gather"]["gathering"] is False
        assert state["sensors"][0]["device_id"] == first["device_id"]
        client.post("/api/sim/press/1", headers=GUI_AUTH)
        assert _connected(client)["device_id"] == first["device_id"]
        # The explicit release is durable, unlike lifespan shutdown.
        assert client.post("/api/release", json={}, headers=GUI_AUTH).status_code == 204
        assert _state(client)["sensors"] == []
    with TestClient(_app(tmp_path, sim), base_url=GUI_BASE) as client:
        client.post("/api/sim/press/1", headers=GUI_AUTH)
        time.sleep(1.1)
        assert _state(client)["sensors"] == []


def test_browser_transport_does_not_start_server_recovery(tmp_path):
    app = _app(tmp_path, SimFleet(1), transport="browser")
    with TestClient(app, base_url=GUI_BASE):
        assert app.state.hub.fleet.recovery_enabled is False
