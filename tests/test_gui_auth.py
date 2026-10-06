"""ms605 gui: token (Bearer / ?t= -> cookie), Host and Origin checks
(docs/GUI_API.md section 4). Every value is synthetic."""

from __future__ import annotations

import pytest
from conftest import GUI_AUTH, GUI_TOKEN
from starlette.websockets import WebSocketDisconnect

from ms605.gui import cli as gui_cli
from ms605.gui.server import cookie_name

LAN_HOSTS = ["127.0.0.1", "localhost", "192.0.2.10", "testhost", "testhost.local"]


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def test_state_needs_a_token(gui):
    response = gui.client.get("/api/state")
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "missing or invalid token"}}
    assert gui.client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert gui.client.get("/api/state", headers={"Authorization": f"Basic {GUI_TOKEN}"}).status_code == 401
    assert gui.client.get("/api/state", headers=GUI_AUTH).status_code == 200


def test_health_needs_no_token(gui):
    response = gui.client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_api_path_needs_the_token_first(gui):
    assert gui.client.get("/api/nope").status_code == 401


def test_query_token_becomes_a_cookie(gui):
    response = gui.client.get(f"/gather?x=1&t={GUI_TOKEN}&y=2", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/gather?x=1&y=2"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"ms605_token_8605={GUI_TOKEN};")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    assert "expires" not in cookie.lower() and "max-age" not in cookie.lower()  # browser-session cookie
    assert gui.client.get("/api/state").status_code == 200  # the cookie alone is enough
    cookie_header = {"Cookie": f"ms605_token_8605={GUI_TOKEN}"}  # what a browser sends on the WS handshake
    with gui.ws(auth=False, headers=cookie_header) as ws:
        assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("//evil.example/x?t=wrong", "/evil.example/x"),
        ("/%2F%2Fevil.example?t=wrong", "/evil.example"),
        ("///evil.example?a=1&t=wrong", "/evil.example?a=1"),
    ],
)
def test_query_token_redirect_stays_on_this_server(gui, path, location):
    response = gui.client.get(f"http://127.0.0.1:8605{path}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == location  # never "//evil.example": a scheme-relative URL


def test_wrong_query_token_redirects_without_a_cookie(gui):
    response = gui.client.get("/?t=wrong", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "set-cookie" not in response.headers
    assert gui.client.get("/api/state").status_code == 401


def test_cookie_of_another_port_does_not_count(gui):
    gui.client.cookies.set("ms605_token_9999", GUI_TOKEN)
    assert gui.client.get("/api/state").status_code == 401


def test_cookie_name_follows_the_host_port():
    assert cookie_name("127.0.0.1:8605") == "ms605_token_8605"
    assert cookie_name("localhost") == "ms605_token_80"
    assert cookie_name("[::1]") == "ms605_token_80"


def test_unknown_host_is_refused(gui):
    response = gui.client.get("/api/health", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert response.text == "Invalid host header"
    assert gui.client.get("/api/health", headers={"Host": "localhost:8605"}).status_code == 200


def test_origin_check_on_writes(gui):
    def stop(origin: str | None):
        headers = {} if origin is None else {"Origin": origin}
        return gui.post("/api/gather/stop", headers=headers)

    for origin in ("http://evil.example", "null", "https://127.0.0.1:8605", "http://127.0.0.1:5173"):
        response = stop(origin)
        assert response.status_code == 403, origin
        assert _error_code(response) == "forbidden_origin"
    assert stop("http://127.0.0.1:8605").status_code == 200
    assert stop(None).status_code == 200
    # a bad Origin is refused before the token is looked at
    assert gui.client.post("/api/gather/stop", headers={"Origin": "http://evil.example"}).status_code == 403
    # reads are not Origin-checked
    assert gui.get("/api/state", headers={"Origin": "http://evil.example"}).status_code == 200


def test_https_origin_matches_secure_http_and_websockets(gui):
    origin = "https://127.0.0.1:8605"
    assert gui.client.post(f"{origin}/api/gather/stop", headers={**GUI_AUTH, "Origin": origin}).status_code == 200
    assert gui.client.post(
        f"{origin}/api/gather/stop", headers={**GUI_AUTH, "Origin": "http://127.0.0.1:8605"}
    ).status_code == 403
    with gui.client.websocket_connect("wss://127.0.0.1:8605/ws", headers={**GUI_AUTH, "Origin": origin}) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_https_login_sets_a_secure_session_cookie(gui):
    response = gui.client.get(f"https://127.0.0.1:8605/?t={GUI_TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]
    assert gui.client.get("https://127.0.0.1:8605/api/state").status_code == 200
    assert gui.client.get("http://127.0.0.1:8605/api/state").status_code == 401


def test_ws_without_token_closes_4401(gui):
    with gui.ws(auth=False) as ws, pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    assert info.value.code == 4401


def test_ws_with_a_foreign_origin_closes_4403(gui):
    with gui.ws(headers={"Origin": "http://evil.example"}) as ws, pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    assert info.value.code == 4403


def test_lan_hosts_and_origin(make_gui):
    rig = make_gui(allowed_hosts=LAN_HOSTS, lan=True)
    lan = {"Host": "192.0.2.10:8605"}
    state = rig.get("/api/state", headers=lan)
    assert state.status_code == 200
    assert state.json()["server"]["lan"] is True
    assert rig.get("/api/state", headers={"Host": "testhost.local:8605"}).status_code == 200
    assert rig.get("/api/state", headers={"Host": "198.51.100.7:8605"}).status_code == 400
    assert rig.post("/api/gather/stop", headers={**lan, "Origin": "http://192.0.2.10:8605"}).status_code == 200
    refused = rig.post("/api/gather/stop", headers={**lan, "Origin": "http://127.0.0.1:8605"})
    assert refused.status_code == 403
    assert _error_code(refused) == "forbidden_origin"
    # the LAN cookie is named after the LAN URL's port
    response = rig.client.get(f"/?t={GUI_TOKEN}", headers=lan, follow_redirects=False)
    assert response.headers["set-cookie"].startswith("ms605_token_8605=")


def test_allowed_hosts(monkeypatch):
    monkeypatch.setattr(gui_cli.socket, "gethostname", lambda: "testhost")
    assert gui_cli.allowed_hosts(False, "192.0.2.10") == ["127.0.0.1", "localhost"]
    assert gui_cli.allowed_hosts(True, "192.0.2.10") == LAN_HOSTS
    assert gui_cli.allowed_hosts(True, None) == ["127.0.0.1", "localhost", "testhost", "testhost.local"]


def test_allowed_hosts_takes_every_lan_address(monkeypatch):
    monkeypatch.setattr(gui_cli.socket, "gethostname", lambda: "testhost")
    hosts = gui_cli.allowed_hosts(True, "192.0.2.20", "192.0.2.10")  # --lan-host, then the default-route address
    assert hosts == ["127.0.0.1", "localhost", "192.0.2.20", "192.0.2.10", "testhost", "testhost.local"]
    assert gui_cli.allowed_hosts(True, "192.0.2.10", "192.0.2.10") == LAN_HOSTS


def test_ws_with_a_foreign_host_is_closed_not_denied(make_gui):
    rig = make_gui(allowed_hosts=LAN_HOSTS, lan=True)
    with (
        pytest.raises(WebSocketDisconnect) as info,
        rig.client.websocket_connect("/ws", headers={"Host": "evil.example:8605", **GUI_AUTH}) as ws,
    ):
        ws.receive_json()
    assert info.value.code == 1008


def test_allowed_hosts_normalises_the_host_name(monkeypatch):
    monkeypatch.setattr(gui_cli.socket, "gethostname", lambda: "Lab-MacBook.local")
    assert gui_cli.allowed_hosts(True, None) == ["127.0.0.1", "localhost", "lab-macbook", "lab-macbook.local"]
