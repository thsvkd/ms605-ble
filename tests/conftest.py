"""Shared test fixtures."""

from __future__ import annotations

import pytest

from ms605.driver import MS605


@pytest.fixture
def no_chunk_pacing(monkeypatch):
    """The driver paces BLE chunks 20 ms apart in wall time; the simulator needs
    no pacing. Core objects build their own MS605 (no inter_chunk_delay knob),
    so drop the default for the test instead -- the suite stays fast."""
    monkeypatch.setitem(MS605.__init__.__kwdefaults__, "inter_chunk_delay", 0)


# -- ms605 gui (docs/GUI_API.md section 11) --------------------------------------

GUI_TOKEN = "test-token"
GUI_BASE = "http://127.0.0.1:8605"
GUI_AUTH = {"Authorization": f"Bearer {GUI_TOKEN}"}
GUI_INDEX = "<!doctype html><title>spa</title>"


class GuiRig:
    """One create_app() over a SimFleet, with its TestClient (entered unless asked not to)."""

    def __init__(self, tmp_path, *, sim_count: int, static: bool, **app_kw) -> None:
        from fastapi.testclient import TestClient

        from ms605.fleet import Fleet
        from ms605.gui.server import create_app
        from ms605.registry import Registry
        from ms605.sim import SimFleet
        from ms605.storage import Storage

        self.sim = SimFleet(sim_count, speed=100) if sim_count else None
        seam = self.sim or SimFleet(0)
        self.storage = Storage(root=tmp_path / "data")
        self.registry = Registry(self.storage)
        self.fleet = Fleet(
            self.registry,
            self.storage,
            scan=seam.discover,
            client_factory=seam.client_factory,
            keepalive_interval=0.15,
            gather_pause=0.01,
        )
        self.static_dir = tmp_path / "static"
        if static:
            self.static_dir.mkdir()
            (self.static_dir / "index.html").write_text(GUI_INDEX, encoding="utf-8")
        self.app = create_app(
            self.fleet, self.registry, self.storage, GUI_TOKEN, sim=self.sim, static_dir=self.static_dir, **app_kw
        )
        self.client = TestClient(self.app, base_url=GUI_BASE)

    def post(self, path: str, json=None, **kw):
        return self.client.post(path, json=json, headers={**GUI_AUTH, **kw.pop("headers", {})}, **kw)

    def get(self, path: str, **kw):
        return self.client.get(path, headers={**GUI_AUTH, **kw.pop("headers", {})}, **kw)

    def ws(self, *, auth: bool = True, headers: dict | None = None):
        """TestClient always dials ws://testserver: give the Host (and the token) explicitly."""
        base = {"Host": "127.0.0.1:8605", **(GUI_AUTH if auth else {})}
        return self.client.websocket_connect("/ws", headers={**base, **(headers or {})})


@pytest.fixture
def make_gui(tmp_path, no_chunk_pacing):
    """make_gui(sim_count=3, static=True, enter=True, **create_app kwargs) -> GuiRig."""
    entered = []

    def make(*, sim_count: int = 3, static: bool = True, enter: bool = True, **app_kw) -> GuiRig:
        rig = GuiRig(tmp_path, sim_count=sim_count, static=static, **app_kw)
        if enter:
            rig.client.__enter__()
            entered.append(rig.client)
        return rig

    yield make
    for client in reversed(entered):
        client.__exit__(None, None, None)


@pytest.fixture
def gui(make_gui):
    return make_gui()


def recv_until(ws, predicate, limit: int = 200) -> list[dict]:
    """Read JSON messages until one satisfies `predicate`; return every message read."""
    got = []
    for _ in range(limit):
        message = ws.receive_json()
        got.append(message)
        if predicate(message):
            return got
    raise AssertionError(f"no matching message in {limit}: {[m.get('type') for m in got]}")
