"""Synthetic tailnet discovery and browser access through each advertised host."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ms605.cli.cli import build_parser
from ms605.gui import cli as gui_cli

TAILNET_IP = "100.64.0.10"
TAILNET_DNS = "sensor.test-tailnet.ts.net"
STATUS = {
    "BackendState": "Running",
    "TailscaleIPs": [TAILNET_IP, "fd7a:115c:a1e0::10"],
    "Self": {"DNSName": "Sensor.Test-Tailnet.ts.net."},
    "CurrentTailnet": {"MagicDNSEnabled": True},
}


class Listener:
    def __init__(self, host, port):
        self.address = host, port
        self.closed = False

    def getsockname(self):
        return self.address

    def close(self):
        self.closed = True


def test_share_port_is_lan_only_and_accepts_ephemeral_port():
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args(["gui", "--share-port", "8606"])
    assert info.value.code == 2
    assert build_parser().parse_args(["gui", "--lan", "--share-port", "0"]).share_port == 0


@pytest.fixture
def tailscale_cli(tmp_path, monkeypatch):
    command = tmp_path / "tailscale"
    monkeypatch.setattr(shutil, "which", lambda name: str(command) if name == "tailscale" else None)

    def status(body, exit_code=0):
        # Only synthetic JSON is embedded; execute the real subprocess boundary.
        payload = body if isinstance(body, str) else json.dumps(body)
        command.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\nexit {exit_code}\n")
        command.chmod(0o755)

    return status


def test_tailnet_discovers_ipv4_and_enabled_magicdns(tailscale_cli):
    tailscale_cli(STATUS)
    assert gui_cli.tailnet_hosts() == [TAILNET_IP, TAILNET_DNS]


def test_tailnet_does_not_advertise_disabled_magicdns(tailscale_cli):
    tailscale_cli({**STATUS, "CurrentTailnet": {"MagicDNSEnabled": False}})
    assert gui_cli.tailnet_hosts() == [TAILNET_IP]


@pytest.mark.parametrize("state", ["Stopped", "Starting", "NeedsLogin", "NeedsMachineAuth"])
def test_tailnet_does_not_advertise_disconnected_addresses(tailscale_cli, state):
    tailscale_cli({**STATUS, "BackendState": state})
    assert gui_cli.tailnet_hosts() == []


@pytest.mark.parametrize(
    "body",
    ["not JSON", [], {}, {**STATUS, "TailscaleIPs": []}, {**STATUS, "TailscaleIPs": ["not an IP"]}],
)
def test_tailnet_unavailable_does_not_prevent_gui_startup(tailscale_cli, body):
    tailscale_cli(body)
    assert gui_cli.tailnet_hosts() == []


def test_tailnet_cli_error_does_not_advertise_stale_addresses(tailscale_cli):
    tailscale_cli(STATUS, exit_code=1)
    assert gui_cli.tailnet_hosts() == []


def test_tailnet_missing_cli_is_optional(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    assert gui_cli.tailnet_hosts() == []


def test_tailnet_cli_timeout_is_optional(tailscale_cli, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 2)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert gui_cli.tailnet_hosts() == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS app-bundle CLI fallback")
def test_tailnet_uses_macos_app_cli_when_not_on_path(monkeypatch):
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == app)

    def status(args, **kwargs):
        assert args == [app, "status", "--json", "--peers=false"]
        assert kwargs["env"]["TAILSCALE_BE_CLI"] == "1"
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(args, 0, json.dumps(STATUS), "")

    monkeypatch.setattr(subprocess, "run", status)
    assert gui_cli.tailnet_hosts() == [TAILNET_IP, TAILNET_DNS]


@pytest.mark.parametrize("lan, discovered", [(True, [TAILNET_IP, TAILNET_DNS]), (True, []), (False, [])])
def test_gui_advertised_hosts_allow_cookie_api_and_websockets(tmp_path, monkeypatch, capsys, lan, discovered):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gui_cli, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(gui_cli.secrets, "token_urlsafe", lambda _: "synthetic-token")
    ephemeral_ports = iter((32000, 32001))
    monkeypatch.setattr(
        gui_cli, "_listener", lambda host, port: Listener(host, next(ephemeral_ports) if port == 0 else port)
    )

    def discover():
        assert lan, "local-only mode must not invoke Tailscale"
        return discovered

    monkeypatch.setattr(gui_cli, "tailnet_hosts", discover, raising=False)
    remote_hosts = ["192.0.2.10", *discovered] if lan else []
    listeners = []

    async def serve(server, *, sockets):
        address, port = sockets[0].getsockname()
        listeners.append((server.config, port))
        assert address == ("0.0.0.0" if lan else "127.0.0.1")
        is_https = server.config.ssl_certfile is not None
        if is_https:
            tls_root = tmp_path / "cal_results" / "gui_tls"
            assert Path(server.config.ssl_certfile) == tls_root / "cert.pem"
            assert Path(server.config.ssl_keyfile) == tls_root / "key.pem"
            assert server.config.lifespan == "on"
        else:
            assert lan
            assert server.config.ssl_keyfile is None
            assert server.config.lifespan == "off"
            return
        scheme = "https" if is_https else "http"
        for host in ["127.0.0.1", *remote_hosts]:
            origin = f"{scheme}://{host}:{port}"
            with TestClient(server.config.app, base_url=origin) as client:
                assert client.get("/api/state").status_code == 401
                login = client.get("/?t=synthetic-token", follow_redirects=False)
                assert login.status_code == 303
                assert "HttpOnly" in login.headers["set-cookie"]
                assert client.get("/api/state").status_code == 200
                assert client.post("/api/gather/stop", headers={"Origin": origin}).status_code == 200
                ws_scheme = "wss" if is_https else "ws"
                with client.websocket_connect(f"{ws_scheme}://{host}:{port}/ws", headers={"Origin": origin}) as ws:
                    assert ws.receive_json()["type"] == "snapshot"
                assert client.post("/api/gather/stop", headers={"Origin": "https://evil.example"}).status_code == 403
                with (
                    client.websocket_connect(
                        f"{ws_scheme}://{host}:{port}/ws", headers={"Origin": f"{scheme}://evil.example"}
                    ) as ws,
                    pytest.raises(WebSocketDisconnect) as info,
                ):
                    ws.receive_json()
                assert info.value.code == 4403
                assert client.get("/api/health", headers={"Host": "unlisted.ts.net"}).status_code == 400
    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    argv = ["gui", "--sim", "1", "--port", "0", *(["--lan"] if lan else [])]
    args = build_parser().parse_args(argv)
    assert asyncio.run(gui_cli.run_gui(args, scan_secs=1, connect_timeout=1)) == 0
    output = capsys.readouterr().out
    assert output.startswith("ms605 gui: https://127.0.0.1:")
    https_port = next(port for config, port in listeners if config.ssl_certfile is not None)
    for host in remote_hosts:
        assert f"https://{host}:{https_port}/?t=synthetic-token" in output
    if not lan:
        assert len(listeners) == 1
        assert "LAN 주소:" not in output and "Tailnet 주소:" not in output
    else:
        assert len(listeners) == 2
        http_port = next(port for config, port in listeners if config.ssl_certfile is None)
        assert http_port == https_port + 1
        assert f"로컬 HTTP 주소: http://127.0.0.1:{http_port}/?t=synthetic-token" in output
        assert f"LAN 공유 주소: http://192.0.2.10:{http_port}/?t=synthetic-token" in output
        if not discovered:
            assert "Tailnet 주소를 찾지 못했습니다" in output


def test_lan_share_bind_failure_starts_neither_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gui_cli, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(gui_cli, "tailnet_hosts", lambda: [])
    calls = []

    async def serve(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    primary = Listener("0.0.0.0", 32000)

    def listener(host, port):
        if port == 32001:
            raise OSError("already in use")
        assert (host, port) == ("0.0.0.0", 0)
        return primary

    monkeypatch.setattr(gui_cli, "_listener", listener)
    args = build_parser().parse_args(
        ["gui", "--lan", "--sim", "1", "--port", "0", "--share-port", "32001"]
    )
    assert asyncio.run(gui_cli.run_gui(args, scan_secs=1, connect_timeout=1)) == 2
    assert calls == []
    assert primary.closed
    assert "공유 포트 32001을(를) 열 수 없습니다" in capsys.readouterr().err


def test_max_https_port_uses_an_ephemeral_share_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gui_cli, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(gui_cli, "tailnet_hosts", lambda: [])
    requested = []

    def listener(host, port):
        requested.append((host, port))
        return Listener(host, 32000 if port == 0 else port)

    async def serve(*_args, **_kwargs):
        pass

    monkeypatch.setattr(gui_cli, "_listener", listener)
    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    args = build_parser().parse_args(["gui", "--lan", "--sim", "1", "--port", "65535"])
    assert asyncio.run(gui_cli.run_gui(args, scan_secs=1, connect_timeout=1)) == 0
    assert requested == [("0.0.0.0", 65535), ("0.0.0.0", 0)]
