"""GUI transport selection and HTTPS startup without touching a physical adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from conftest import GUI_AUTH, GUI_TOKEN
from starlette.websockets import WebSocketDisconnect

from ms605.cli.cli import build_parser
from ms605.driver import MS605
from ms605.gui import cli as gui_cli


class Listener:
    def setsockopt(self, *_args):
        pass

    def bind(self, address):
        self.address = address

    def listen(self, _backlog):
        pass

    def getsockname(self):
        return self.address[0], 8605

    def close(self):
        pass


@pytest.mark.parametrize("explicit_tls", [False, True])
def test_gui_uses_only_browser_bluetooth_and_advertises_tls(tmp_path, monkeypatch, capsys, explicit_tls):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    socket = gui_cli.socket
    monkeypatch.setattr(gui_cli, "socket", SimpleNamespace(
        socket=lambda *_args: Listener(), AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR, gethostname=lambda: "synthetic-host",
    ))
    monkeypatch.setattr(gui_cli, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(gui_cli, "tailnet_hosts", lambda: [])
    monkeypatch.setattr(gui_cli.secrets, "token_urlsafe", lambda _size: GUI_TOKEN)
    scans = []

    async def scan(*_args):
        scans.append(True)
        return []

    monkeypatch.setattr(MS605, "scan", scan)

    async def serve(server, *, sockets):
        app = server.config.app
        if server.config.ssl_certfile is None:
            assert server.config.lifespan == "off"
            return
        tls_root = tmp_path / "cal_results" / "gui_tls"
        assert Path(server.config.ssl_certfile) == (Path("cert.pem") if explicit_tls else tls_root / "cert.pem")
        assert Path(server.config.ssl_keyfile) == (Path("key.pem") if explicit_tls else tls_root / "key.pem")
        async with app.router.lifespan_context(app):
            assert app.state.browser_ble is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://127.0.0.1:8605"
            ) as client:
                response = await client.post("/api/gather/start", headers=GUI_AUTH)
                assert response.status_code == 200
                assert response.json()["gathering"] is True
                await asyncio.sleep(0.03)
                assert not scans, "GUI must never scan the server's Bluetooth adapter"
                assert (await client.post("/api/gather/stop", headers=GUI_AUTH)).status_code == 200

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    argv = [
        "gui", "--lan",
        *(["--ssl-certfile", "cert.pem", "--ssl-keyfile", "key.pem"] if explicit_tls else []),
    ]
    args = build_parser().parse_args(argv)
    assert asyncio.run(gui_cli.run_gui(args, scan_secs=0.01, connect_timeout=1)) == 0
    output = capsys.readouterr().out
    assert "ms605 gui: https://127.0.0.1:8605/" in output
    assert "LAN 주소: https://192.0.2.10:8605/" in output


def test_server_ble_transport_scans_the_server_adapter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    socket = gui_cli.socket
    monkeypatch.setattr(gui_cli, "socket", SimpleNamespace(
        socket=lambda *_args: Listener(), AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR, gethostname=lambda: "synthetic-host",
    ))
    monkeypatch.setattr(gui_cli.secrets, "token_urlsafe", lambda _size: GUI_TOKEN)
    scans = []

    async def scan(duration):
        scans.append(duration)
        return []

    monkeypatch.setattr(MS605, "scan", scan)

    async def serve(server, *, sockets):
        app = server.config.app
        async with app.router.lifespan_context(app):
            assert app.state.browser_ble is None
            assert app.state.hub.server.ble_transport == "server"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://127.0.0.1:8605"
            ) as client:
                response = await client.post("/api/gather/start", headers=GUI_AUTH)
                assert response.status_code == 200
                await asyncio.sleep(0.03)
                assert scans == [0.01]
                assert (await client.post("/api/gather/stop", headers=GUI_AUTH)).status_code == 200

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    args = build_parser().parse_args([
        "gui", "--ble-transport", "server",
        "--ssl-certfile", "cert.pem", "--ssl-keyfile", "key.pem",
    ])
    assert asyncio.run(gui_cli.run_gui(args, scan_secs=0.01, connect_timeout=1)) == 0
    assert "GUI 서버 컴퓨터의 Bluetooth로 연결" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("headers", "code"),
    [({}, 4401), ({**GUI_AUTH, "Origin": "https://evil.example"}, 4403)],
)
def test_browser_ble_socket_checks_auth_and_origin(gui, headers, code):
    with pytest.raises(WebSocketDisconnect) as info:
        with gui.client.websocket_connect("/ws/ble", headers={"Host": "127.0.0.1:8605", **headers}) as ws:
            ws.receive_json()
    assert info.value.code == code
