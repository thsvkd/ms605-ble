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

    def discover():
        assert lan, "local-only mode must not invoke Tailscale"
        return discovered

    monkeypatch.setattr(gui_cli, "tailnet_hosts", discover, raising=False)
    remote_hosts = ["192.0.2.10", *discovered] if lan else []
    bound_ports = []

    async def serve(server, *, sockets):
        address, port = sockets[0].getsockname()
        bound_ports.append(port)
        assert address == ("0.0.0.0" if lan else "127.0.0.1")
        for host in ["127.0.0.1", *remote_hosts]:
            origin = f"http://{host}:{port}"
            with TestClient(server.config.app, base_url=origin) as client:
                assert client.get("/api/state").status_code == 401
                login = client.get("/?t=synthetic-token", follow_redirects=False)
                assert login.status_code == 303
                assert "HttpOnly" in login.headers["set-cookie"]
                assert client.get("/api/state").status_code == 200
                assert client.post("/api/gather/stop", headers={"Origin": origin}).status_code == 200
                with client.websocket_connect(f"ws://{host}:{port}/ws", headers={"Origin": origin}) as ws:
                    assert ws.receive_json()["type"] == "snapshot"
                assert client.post("/api/gather/stop", headers={"Origin": "http://evil.example"}).status_code == 403
                with (
                    client.websocket_connect(f"ws://{host}:{port}/ws", headers={"Origin": "http://evil.example"}) as ws,
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
    assert output.startswith("ms605 gui: http://127.0.0.1:")
    for host in remote_hosts:
        assert f"http://{host}:{bound_ports[0]}/?t=synthetic-token" in output
    if not lan:
        assert "LAN 주소:" not in output and "Tailnet 주소:" not in output
    elif not discovered:
        assert "Tailnet 주소를 찾지 못했습니다" in output
