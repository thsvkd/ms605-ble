"""ms605 gui REST API, SPA fallback and the `gui` subcommand's start-up errors
(docs/GUI_API.md sections 6, 8). Every value is synthetic."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import tempfile
import time

import pytest
from conftest import GUI_AUTH, GUI_BASE, GUI_INDEX
from fastapi.testclient import TestClient

from ms605.cli.cli import build_parser
from ms605.gui.cli import run_gui
from ms605.gui.server import SPA_MISSING, slugify, unique_site_id

SIM1 = "53494d3630350001"  # b"SIM605" + 1
SIM2 = "53494d3630350002"
GOOD_YAML = "# synthetic\nSensor A: 02:00:00:00:00:0A\n\nSensor B: '02:00:00:00:00:0B'\n"


def _code(response) -> str:
    return response.json()["error"]["code"]


def _registry_file(gui) -> dict:
    return json.loads(gui.storage.registry_path.read_text(encoding="utf-8"))


def _wait_connected(gui, device_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sensor in gui.get("/api/state").json()["sensors"]:
            if sensor["device_id"] == device_id and sensor["live"] and sensor["live"]["link"] == "connected":
                return sensor
        time.sleep(0.02)
    raise AssertionError(f"{device_id} never connected")


# -- state ----------------------------------------------------------------------


def test_snapshot_shape(gui):
    body = gui.get("/api/state").json()
    assert body["seq"] == 0
    assert body["server"]["sim"] == {"count": 3, "speed": 100.0}
    assert body["server"]["lan"] is False
    assert body["gather"] == {"gathering": False, "connecting": []}
    assert body["sites"] == body["sensors"] == body["pending"] == []


def test_health_reports_the_version(gui):
    assert gui.client.get("/api/health").json() == {"status": "ok", "version": gui.app.state.hub.server.version}


# -- sites ------------------------------------------------------------------------


def test_site_slugs(gui):
    def create(**body):
        return gui.post("/api/sites", body)

    first = create(name="Lab A")
    assert first.status_code == 201
    assert first.json() == {"site_id": "lab-a", "name": "Lab A"}
    assert create(name="회의실").json()["site_id"] == "site"
    assert create(name="Lab  A!").json()["site_id"] == "lab-a-2"
    assert create(name="3층 사무실").json()["site_id"] == "3"
    assert _registry_file(gui)["sites"]["lab-a"] == {"name": "Lab A"}


def test_explicit_site_id(gui):
    assert gui.post("/api/sites", {"name": "Lab", "site_id": "lab-x"}).json()["site_id"] == "lab-x"
    dup = gui.post("/api/sites", {"name": "Other", "site_id": "lab-x"})
    assert dup.status_code == 409
    assert _code(dup) == "already_exists"
    bad = gui.post("/api/sites", {"name": "Lab", "site_id": "Lab_X"})
    assert bad.status_code == 422
    assert _code(bad) == "invalid_request"
    assert "site_id" in bad.json()["error"]["message"]


def test_slug_rules():
    assert slugify("  --Lab A--  ") == "lab-a"
    assert slugify("x" * 40) == "x" * 32
    assert slugify("a" * 31 + " b") == "a" * 31  # cut at 32, then the trailing "-" goes
    base = "y" * 32
    assert unique_site_id(base, {base}) == "y" * 30 + "-2"
    assert unique_site_id("lab", {"lab", "lab-2"}) == "lab-3"


# -- sensors ----------------------------------------------------------------------


def test_sensor_crud(gui):
    gui.post("/api/sites", {"name": "Lab A"})
    gui.post("/api/sites", {"name": "Lab B"})
    missing_site = gui.post("/api/sensors", {"device_id": SIM1, "site_id": "nope", "alias": "S1"})
    assert missing_site.status_code == 404
    assert _code(missing_site) == "not_found"

    created = gui.post(
        "/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": " Sensor 1 ", "location": "north wall"}
    )
    assert created.status_code == 201
    view = created.json()
    assert view["device_id"] == SIM1 and view["live"] is None
    assert view["registry"]["alias"] == "Sensor 1"  # stripped
    assert view["registry"]["location"] == "north wall"
    assert view["last_calibration"] is None and view["last_snapshot"] is None
    assert _registry_file(gui)["sensors"][SIM1]["alias"] == "Sensor 1"

    dup = gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "again"})
    assert dup.status_code == 409
    assert _code(dup) == "already_exists"
    for body in (
        {"device_id": SIM2, "site_id": "lab-a", "alias": "   "},
        {"device_id": SIM2, "site_id": "lab-a", "alias": "S2", "color": "red"},
        {"device_id": "XYZ", "site_id": "lab-a", "alias": "S2"},
    ):
        bad = gui.post("/api/sensors", body)
        assert bad.status_code == 422, body
        assert _code(bad) == "invalid_request"

    assert gui.client.patch(f"/api/sensors/{SIM1}", json={}, headers=GUI_AUTH).status_code == 200
    patched = gui.client.patch(f"/api/sensors/{SIM1}", json={"site_id": "lab-b", "notes": "n"}, headers=GUI_AUTH)
    assert patched.status_code == 200
    assert patched.json()["registry"] | {"last_seen": None} == {
        "site_id": "lab-b", "alias": "Sensor 1", "location": "north wall", "notes": "n",
        "last_seen": None, "battery_pct": None,
    }  # fmt: skip
    assert gui.client.patch(f"/api/sensors/{SIM2}", json={}, headers=GUI_AUTH).status_code == 404
    no_site = gui.client.patch(f"/api/sensors/{SIM1}", json={"site_id": "nope"}, headers=GUI_AUTH)
    assert no_site.status_code == 404
    assert _code(no_site) == "not_found"

    assert gui.client.delete(f"/api/sensors/{SIM1}", headers=GUI_AUTH).status_code == 204
    assert gui.get("/api/state").json()["sensors"] == []
    assert SIM1 not in _registry_file(gui)["sensors"]
    again = gui.client.delete(f"/api/sensors/{SIM1}", headers=GUI_AUTH)
    assert again.status_code == 404
    assert _code(again) == "not_found"


# -- import -----------------------------------------------------------------------


def test_import_sensor_info(gui):
    body = {"filename": "sensor_info_example.yaml", "content": GOOD_YAML}
    first = gui.post("/api/import/sensor-info", body)
    assert first.status_code == 200
    result = first.json()
    assert result["site_id"] == "example"
    source = "sensor_info_example.yaml"
    assert result["added"] == [
        {"site_id": "example", "alias": "Sensor A", "address": "02:00:00:00:00:0A", "source": source},
        {"site_id": "example", "alias": "Sensor B", "address": "02:00:00:00:00:0B", "source": source},
    ]
    state = gui.get("/api/state").json()
    assert state["sites"] == [{"site_id": "example", "name": "example"}]
    assert len(state["pending"]) == 2
    assert gui.post("/api/import/sensor-info", body).json() == {"site_id": "example", "added": []}


def test_import_site_from_name_and_path_stripped(gui):
    body = {"filename": "../../x/list.yaml", "content": GOOD_YAML, "site_name": "Lab A"}
    result = gui.post("/api/import/sensor-info", body).json()
    assert result["site_id"] == "lab-a"
    assert {p["source"] for p in result["added"]} == {"list.yaml"}
    assert gui.get("/api/state").json()["sites"] == [{"site_id": "lab-a", "name": "Lab A"}]
    unnamed = {"filename": "..", "content": "C: 02:00:00:00:00:0C", "site_id": "lab-a"}
    explicit = gui.post("/api/import/sensor-info", unnamed)
    assert explicit.json()["added"][0]["source"] == "sensor_info.yaml"


def test_import_korean_site_names_do_not_collapse_into_one_site(gui):
    def imp(content, site_name):
        return gui.post("/api/import/sensor-info", {"filename": "x.yaml", "content": content, "site_name": site_name})

    first = imp(GOOD_YAML, "연구실")
    second = imp("C: 02:00:00:00:00:0C", "회의실")
    assert first.json()["site_id"] != second.json()["site_id"]
    again = imp("D: 02:00:00:00:00:0D", "회의실")
    assert again.json()["site_id"] == second.json()["site_id"]  # the same name reuses its site
    names = {s["site_id"]: s["name"] for s in gui.get("/api/state").json()["sites"]}
    assert names == {first.json()["site_id"]: "연구실", second.json()["site_id"]: "회의실"}


def test_import_explicit_site_id_must_be_existing_or_a_slug(gui):
    body = {"filename": "list.yaml", "content": GOOD_YAML, "site_id": "Weird Id/../x"}
    response = gui.post("/api/import/sensor-info", body)
    assert response.status_code == 422
    assert _code(response) == "invalid_request"
    assert gui.get("/api/state").json()["sites"] == []
    gui.registry.add_site("Old Site", "made by the CLI")  # an existing id is taken as it is
    response = gui.post("/api/import/sensor-info", {**body, "site_id": "Old Site"})
    assert response.status_code == 200
    assert response.json()["site_id"] == "Old Site"
    created = gui.post("/api/import/sensor-info", {**body, "site_id": "lab-b"})
    assert created.status_code == 200
    assert "lab-b" in gui.registry.sites


def test_import_bad_line(gui):
    body = {"filename": "sensor_info_bad.yaml", "content": "A: 02:00:00:00:00:0A\n# ok\nx\n"}
    response = gui.post("/api/import/sensor-info", body)
    assert response.status_code == 422
    assert _code(response) == "invalid_file"
    message = response.json()["error"]["message"]
    assert message == "sensor_info_bad.yaml:3: expected 'name: address', got 'x'"
    assert tempfile.gettempdir() not in message
    state = gui.get("/api/state").json()
    assert state["sites"] == [] and state["pending"] == []  # all or nothing
    too_big = gui.post("/api/import/sensor-info", {"filename": "a.yaml", "content": "x" * 70000})
    assert too_big.status_code == 422
    assert _code(too_big) == "invalid_request"


# -- gather, release, sim ------------------------------------------------------------


def test_gather_start_stop(gui):
    assert gui.post("/api/gather/start").json() == {"gathering": True, "connecting": []}
    assert gui.post("/api/gather/start").json()["gathering"] is True
    assert gui.app.state.hub.fleet.gathering
    assert gui.post("/api/gather/stop").json() == {"gathering": False, "connecting": []}
    assert gui.post("/api/gather/stop").json()["gathering"] is False


def test_release_unknown_id_closes_nothing(gui):
    gui.post("/api/gather/start")
    assert gui.post("/api/sim/press/1").status_code == 204
    _wait_connected(gui, SIM1)
    gui.post("/api/gather/stop")
    response = gui.post("/api/release", {"device_ids": [SIM1, "ffff"]})
    assert response.status_code == 404
    assert _code(response) == "not_found"
    assert _wait_connected(gui, SIM1)["live"]["link"] == "connected"
    assert gui.sim.devices[0].connected
    assert gui.post("/api/release", {"device_ids": [SIM1]}).status_code == 204
    assert gui.get("/api/state").json()["sensors"] == []
    assert not gui.sim.devices[0].connected
    assert gui.post("/api/release", {}).status_code == 204  # nothing left: still fine


def _sessions(gui) -> set[str]:
    return {s["device_id"] for s in gui.get("/api/state").json()["sensors"] if s["live"]}


def test_release_all_while_gathering_stops_gathering(gui):
    gui.post("/api/gather/start")
    gui.post("/api/sim/press-all")
    for device_id in (SIM1, SIM2):
        _wait_connected(gui, device_id)
    assert gui.post("/api/release", {}).status_code == 204
    assert gui.get("/api/state").json()["gather"] == {"gathering": False, "connecting": []}
    time.sleep(0.3)  # ~30 scans at gather_pause=0.01 if gathering had kept running
    assert all(dev.advertising for dev in gui.sim.devices)  # the button window is still open
    assert _sessions(gui) == set()
    assert not any(dev.connected for dev in gui.sim.devices)


def test_release_one_while_gathering_is_not_reconnected_by_that_run(gui):
    gui.post("/api/gather/start")
    gui.post("/api/sim/press/1")
    gui.post("/api/sim/press/2")
    _wait_connected(gui, SIM1)
    _wait_connected(gui, SIM2)
    assert gui.post("/api/release", {"device_ids": [SIM1]}).status_code == 204
    time.sleep(0.3)
    assert gui.sim.devices[0].advertising
    assert _sessions(gui) == {SIM2}
    assert gui.get("/api/state").json()["gather"]["gathering"] is True
    gui.post("/api/gather/start")  # already running: still skips it
    time.sleep(0.1)
    assert _sessions(gui) == {SIM2}
    # a new gather run takes it back
    gui.post("/api/gather/stop")
    gui.post("/api/sim/press/1")
    gui.post("/api/gather/start")
    _wait_connected(gui, SIM1)


def test_sim_routes(gui):
    for path in ("/api/sim/press/0", "/api/sim/press/4", "/api/sim/drop/4"):
        response = gui.post(path)
        assert response.status_code == 404, path
        assert _code(response) == "not_found"
    assert gui.post("/api/sim/press-all").status_code == 204
    assert all(dev.advertising for dev in gui.sim.devices)
    assert gui.post("/api/sim/drop/1").status_code == 204  # not connected: a no-op


def test_sim_routes_absent_without_sim(make_gui):
    rig = make_gui(sim_count=0)
    assert rig.get("/api/state").json()["server"]["sim"] is None
    for path in ("/api/sim/press/1", "/api/sim/press-all", "/api/sim/drop/1"):
        response = rig.post(path)
        assert response.status_code == 404, path
        assert _code(response) == "not_found"


def test_unexpected_key_error_is_500_not_404(gui, monkeypatch):
    gui.post("/api/sites", {"name": "Lab A"})
    assert gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "A"}).status_code == 201

    def broken(device_id):
        raise KeyError("bug")

    monkeypatch.setattr(gui.app.state.hub, "sensor_view", broken)
    client = TestClient(gui.app, base_url=GUI_BASE, raise_server_exceptions=False)  # the app's lifespan is gui's
    response = client.patch(f"/api/sensors/{SIM1}", json={"notes": "x"}, headers=GUI_AUTH)
    assert response.status_code == 500
    assert _code(response) == "internal"


def test_unreadable_snapshot_does_not_fail_a_saved_change(gui, monkeypatch):
    gui.post("/api/sites", {"name": "Lab A"})
    gui.post("/api/sensors", {"device_id": SIM1, "site_id": "lab-a", "alias": "A"})
    snap_dir = gui.storage.snapshots_dir / SIM1
    snap_dir.mkdir(parents=True)
    (snap_dir / "20260101-000000.json").write_text("{}", encoding="utf-8")

    def denied(*_a, **_kw):
        raise PermissionError("synthetic: permission denied")

    monkeypatch.setattr(gui.storage, "load_snapshot", denied)
    response = gui.client.patch(f"/api/sensors/{SIM1}", json={"notes": "x"}, headers=GUI_AUTH)
    assert response.status_code == 200
    assert response.json()["last_snapshot"] is None
    assert response.json()["registry"]["notes"] == "x"


def test_request_body_cap(gui):
    big = json.dumps({"filename": "a.yaml", "content": "x" * (600 * 1024)})
    headers = {"Content-Type": "application/json"}
    response = gui.client.post("/api/import/sensor-info", content=big, headers={**GUI_AUTH, **headers})
    assert response.status_code == 413
    assert _code(response) == "invalid_request"
    # the token is still checked first
    assert gui.client.post("/api/import/sensor-info", content=big, headers=headers).status_code == 401


def test_unknown_api_paths(gui):
    for method in ("GET", "POST", "DELETE"):
        response = gui.client.request(method, "/api/nope", headers=GUI_AUTH)
        assert response.status_code == 404, method
        assert _code(response) == "not_found"
    wrong_method = gui.client.put(f"/api/sensors/{SIM1}", json={}, headers=GUI_AUTH)
    assert wrong_method.status_code == 405
    assert _code(wrong_method) == "invalid_request"


# -- SPA and static files --------------------------------------------------------------


def test_spa_fallback_and_static_files(gui, tmp_path):
    (gui.static_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (gui.static_dir / "assets").mkdir()
    (gui.static_dir / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("synthetic secret", encoding="utf-8")

    for path in ("/", "/gather", f"/sensors/{SIM1}", "/nowhere/deep"):
        response = gui.client.get(path)
        assert response.status_code == 200, path
        assert response.text == GUI_INDEX
        assert response.headers["cache-control"] == "no-store"
    assert gui.client.get("/favicon.svg").text == "<svg/>"
    for path in ("/", "/gather", "/favicon.svg"):  # link checkers and QR apps may send HEAD first
        head = gui.client.head(path)
        assert head.status_code == 200, path
        assert head.content == b""
    asset = gui.client.get("/assets/app-abc123.js")
    assert asset.text == "console.log(1)"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert gui.client.get("/assets/missing.js").status_code == 404
    for path in ("/assets/../../secret.txt", "/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
        response = gui.client.get(path)
        assert "synthetic secret" not in response.text, path


def test_static_missing(make_gui):
    rig = make_gui(static=False)
    response = rig.client.get("/")
    assert response.status_code == 503
    assert response.text == SPA_MISSING
    assert rig.client.get("/api/health").status_code == 200


# -- `ms605 gui` start-up --------------------------------------------------------------


def _gui_args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["gui", *argv])


def test_lan_host_needs_lan():
    with pytest.raises(SystemExit) as info:
        _gui_args("--lan-host", "192.0.2.20")
    assert info.value.code == 2
    assert _gui_args("--lan", "--lan-host", "192.0.2.20").lan_host == "192.0.2.20"


def test_speed_needs_sim():
    with pytest.raises(SystemExit) as info:
        _gui_args("--speed", "2")
    assert info.value.code == 2
    assert _gui_args("--sim", "2", "--speed", "2").speed == 2.0
    for bad in (["--sim", "0"], ["--sim", "33"], ["--port", "70000"], ["--sim", "1", "--speed", "0"]):
        with pytest.raises(SystemExit):
            _gui_args(*bad)


def test_gui_tls_requires_both_certificate_and_key():
    for argv in (("--ssl-certfile", "cert.pem"), ("--ssl-keyfile", "key.pem")):
        with pytest.raises(SystemExit) as info:
            _gui_args(*argv)
        assert info.value.code == 2
    args = _gui_args("--ssl-certfile", "cert.pem", "--ssl-keyfile", "key.pem")
    assert args.ssl_certfile == "cert.pem"
    assert args.ssl_keyfile == "key.pem"


def test_port_in_use(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        code = asyncio.run(run_gui(_gui_args("--sim", "1", "--port", str(port)), scan_secs=1, connect_timeout=1))
    assert code == 2
    assert f"포트 {port}을(를) 열 수 없습니다" in capsys.readouterr().err


def test_broken_registry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))
    registry = tmp_path / "cal_results" / "sim" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    code = asyncio.run(run_gui(_gui_args("--sim", "1", "--port", "0"), scan_secs=1, connect_timeout=1))
    assert code == 2
    assert "레지스트리 파일 오류" in capsys.readouterr().err
