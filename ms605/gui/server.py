"""ms605.gui.server -- create_app(): token/Host/Origin checks, the REST routes,
error mapping, /ws and the built SPA (docs/GUI_API.md sections 4, 6, 8.4).
Every handler is `async def`: the core runs on this one event loop (8.3)."""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import logging
import re
import secrets
import tempfile
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ms605.errors import MS605ConnectionError, SessionBusyError, StorageError
from ms605.fleet import Fleet
from ms605.registry import Registry
from ms605.sim import SimFleet
from ms605.storage import Storage

from .schemas import (
    NEW_SITE_ID_PATTERN,
    ApiError,
    ErrorBody,
    GatherStatus,
    Health,
    ImportResult,
    PendingView,
    ReleaseRequest,
    SensorCreate,
    SensorInfoImport,
    SensorUpdate,
    ServerInfo,
    SimInfo,
    SiteCreate,
    SiteView,
    StateSnapshot,
)
from .ws import Hub

_log = logging.getLogger(__name__)

SPA_MISSING = "웹 UI가 빌드되지 않았습니다. cd web && npm ci && npm run build"
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_SITE_ID_MAX = 32
MAX_BODY_BYTES = 512 * 1024  # the largest body, an import of 65536 characters, stays well below this
_NEW_SITE_ID = re.compile(NEW_SITE_ID_PATTERN)


class ApiFailure(Exception):
    """An error with its HTTP status and ApiError code decided by the handler."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ApiError(error=ErrorBody(code=code, message=message))
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def _json(model, status: int = 200) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), status_code=status)


@contextmanager
def _not_found():
    """A KeyError from a registry lookup -> 404. Any other KeyError is a bug (500)."""
    try:
        yield
    except KeyError as exc:
        raise ApiFailure(404, "not_found", f"not found: {exc.args[0] if exc.args else ''}") from exc


def server_version() -> str:
    try:
        return importlib.metadata.version("ms605-ble")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


# -- site ids (6.3, 6.5) -------------------------------------------------------------


def slugify(name: str) -> str:
    """Lowercase, runs of non-[a-z0-9] -> "-", trimmed, at most 32 chars; "site" if empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:_SITE_ID_MAX].rstrip("-")
    return slug or "site"


def unique_site_id(base: str, existing: Collection[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = base[: _SITE_ID_MAX - len(suffix)].rstrip("-") + suffix
        if candidate not in existing:
            return candidate
        n += 1


# -- auth (4.2, 4.3) ------------------------------------------------------------------


def cookie_name(host: str) -> str:
    """`ms605_token_<port>`, the port taken from the Host header (80 if none)."""
    _, sep, port = host.rpartition(":")
    return f"ms605_token_{port if sep and port.isdigit() else '80'}"


def _token_ok(conn: HTTPConnection, token: str) -> bool:
    expected = token.encode()
    scheme, _, value = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and secrets.compare_digest(value.strip().encode(), expected):
        return True
    cookie = conn.cookies.get(cookie_name(conn.headers.get("host", "")))
    return cookie is not None and secrets.compare_digest(cookie.encode(), expected)


def _origin_ok(conn: HTTPConnection) -> bool:
    origin = conn.headers.get("origin")
    return origin is None or origin == f"http://{conn.headers.get('host', '')}"


def _length_ok(conn: HTTPConnection) -> bool:
    """Content-Length, when sent, within MAX_BODY_BYTES (checked before the body is read)."""
    length = conn.headers.get("content-length", "")
    return not length.isdigit() or int(length) <= MAX_BODY_BYTES


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class _Guard:
    """Origin and token checks for /api/* (the /ws handler does its own)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_api(scope["path"]):
            conn = HTTPConnection(scope)
            response = None
            if scope["method"] not in _SAFE_METHODS and not _origin_ok(conn):
                response = _error(403, "forbidden_origin", "Origin does not match Host")
            elif scope["path"] != "/api/health" and not _token_ok(conn, self.token):
                response = _error(401, "unauthorized", "missing or invalid token")
            elif not _length_ok(conn):
                response = _error(413, "invalid_request", f"request body over {MAX_BODY_BYTES} bytes")
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _TrustedHost(TrustedHostMiddleware):
    """A refused /ws handshake is closed (1008; uvicorn answers HTTP 403) instead of
    the middleware's 400 denial response, which uvicorn logs as an ERROR every time."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            await super().__call__(scope, receive, send)
            return

        async def close_instead_of_deny(message) -> None:
            if message["type"] == "websocket.http.response.start":
                await send({"type": "websocket.close", "code": 1008})
            elif message["type"] != "websocket.http.response.body":
                await send(message)

        await super().__call__(scope, receive, close_instead_of_deny)


# -- the app ----------------------------------------------------------------------------


def create_app(
    fleet: Fleet,
    registry: Registry,
    storage: Storage,
    token: str,
    *,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
    sim: SimFleet | None = None,
    lan: bool = False,
    static_dir: Path | None = None,
    ws_queue_size: int = 512,
) -> FastAPI:
    if registry is not fleet.registry or storage is not fleet.storage:
        raise ValueError("registry and storage must be the fleet's own")
    if static_dir is None:
        static_dir = Path(str(importlib.resources.files("ms605.gui") / "static"))
    static_root = static_dir.resolve()
    sim_info = SimInfo(count=len(sim.devices), speed=sim.devices[0].speed) if sim and sim.devices else None
    hub = Hub(fleet, ServerInfo(version=server_version(), lan=lan, sim=sim_info), queue_size=ws_queue_size)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.attach()
        try:
            yield
        finally:
            hub.close_clients()
            try:
                await fleet.aclose()  # stop gathering, release every link (D5)
            finally:
                hub.detach()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.hub = hub
    app.add_middleware(_Guard, token=token)
    app.add_middleware(_TrustedHost, allowed_hosts=list(allowed_hosts))  # outermost: runs first

    # -- errors (6.1) --

    @app.exception_handler(ApiFailure)
    async def _on_api_failure(_r: Request, exc: ApiFailure) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _on_validation(_r: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _error(422, "invalid_request", f"{loc}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(SessionBusyError)
    async def _on_busy(_r: Request, exc: SessionBusyError) -> JSONResponse:
        return _error(409, "busy", str(exc))

    @app.exception_handler(MS605ConnectionError)
    async def _on_not_connected(_r: Request, exc: MS605ConnectionError) -> JSONResponse:
        return _error(409, "not_connected", str(exc))

    @app.exception_handler(StorageError)
    async def _on_storage(_r: Request, exc: StorageError) -> JSONResponse:
        return _error(500, "storage", str(exc))

    @app.exception_handler(ValueError)  # also ProfileError
    async def _on_value(_r: Request, exc: ValueError) -> JSONResponse:
        return _error(422, "invalid", str(exc))

    @app.exception_handler(Exception)
    async def _on_unexpected(_r: Request, exc: Exception) -> JSONResponse:
        _log.error("unhandled error", exc_info=exc)
        return _error(500, "internal", f"{type(exc).__name__}: {exc}")

    # -- REST (6.2) --

    def sensor_response(device_id: str, status: int = 200) -> JSONResponse:
        view = hub.sensor_view(device_id)
        if view is None:
            raise ApiFailure(404, "not_found", f"not found: {device_id}")
        return _json(view, status)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return _json(Health(version=server_version()))

    @app.get("/api/state")
    async def state() -> JSONResponse:
        snapshot: StateSnapshot = hub.snapshot()
        return _json(snapshot)

    @app.post("/api/sites")
    async def create_site(body: SiteCreate) -> JSONResponse:
        site_id = body.site_id or unique_site_id(slugify(body.name), registry.sites)
        try:
            site = registry.add_site(site_id, body.name)
        except ValueError as exc:
            raise ApiFailure(409, "already_exists", str(exc)) from exc
        hub.mark_sites()
        hub.flush()
        return _json(SiteView(site_id=site.site_id, name=site.name), 201)

    @app.post("/api/sensors")
    async def create_sensor(body: SensorCreate) -> JSONResponse:
        try:
            with _not_found():
                registry.add_sensor(body.device_id, body.site_id, body.alias, location=body.location, notes=body.notes)
        except ValueError as exc:
            raise ApiFailure(409, "already_exists", str(exc)) from exc
        session = fleet.sessions.get(body.device_id)
        if session is not None:  # named while connected: cache its address now, as a gather would (11.2 step 7)
            try:
                registry.match(body.device_id, session.address, battery_pct=session.info and session.info.battery_pct)
            except StorageError as exc:  # only the cache refresh failed; the sensor is saved
                _log.warning("registry update for %s failed: %s", body.device_id, exc)
            hub.mark_pending()  # match() drops a pending import of this address
        hub.mark_sensor(body.device_id)
        hub.flush()
        return sensor_response(body.device_id, 201)

    @app.patch("/api/sensors/{device_id}")
    async def update_sensor(device_id: str, body: SensorUpdate) -> JSONResponse:
        with _not_found():
            registry.update_sensor(device_id, **body.model_dump(exclude_none=True))
        hub.mark_sensor(device_id)
        hub.flush()
        return sensor_response(device_id)

    @app.delete("/api/sensors/{device_id}")
    async def delete_sensor(device_id: str) -> Response:
        with _not_found():
            registry.remove_sensor(device_id)
        hub.mark_sensor(device_id)
        hub.flush()
        return Response(status_code=204)

    released: set[str] = set()  # lowercase addresses released during this gather run: not reconnected by it

    @app.post("/api/gather/start")
    async def gather_start() -> JSONResponse:
        if not fleet.gathering:
            released.clear()
            fleet.start_gather(accept=lambda device: device.address.lower() not in released)
            hub.mark_gather()
            hub.flush()
        status: GatherStatus = hub.gather_status()
        return _json(status)

    @app.post("/api/gather/stop")
    async def gather_stop() -> JSONResponse:
        await fleet.stop_gather()
        hub.clear_connecting()
        hub.mark_gather()
        hub.flush()
        return _json(hub.gather_status())

    @app.post("/api/release")
    async def release(body: ReleaseRequest) -> Response:
        if body.device_ids is None:  # everything: stop gathering first, or it reconnects what is still advertising
            await fleet.stop_gather()
            hub.clear_connecting()
            ids = list(fleet.sessions)
        else:
            ids = body.device_ids
            missing = [i for i in ids if i not in fleet.sessions]
            if missing:  # checked before anything is closed
                raise ApiFailure(404, "not_found", f"no session: {', '.join(missing)}")
            if fleet.gathering:  # the button window outlasts the link: keep this gather run off them
                released.update(fleet.sessions[i].address.lower() for i in ids)
        await fleet.release(body.device_ids)
        for device_id in ids:
            hub.mark_sensor(device_id)
        hub.mark_gather()
        hub.flush()
        return Response(status_code=204)

    @app.post("/api/import/sensor-info")
    async def import_sensor_info(body: SensorInfoImport) -> JSONResponse:
        filename = Path(body.filename).name
        if filename in ("", ".", ".."):
            filename = "sensor_info.yaml"
        if body.site_id is not None:
            site_id = body.site_id
            if site_id not in registry.sites and not _NEW_SITE_ID.fullmatch(site_id):
                raise ApiFailure(422, "invalid_request", f"site_id: neither an existing site nor a new id: {site_id!r}")
        elif body.site_name is not None:
            # slugify() maps any all-Korean name to "site": reuse the site with this exact name,
            # otherwise give the new name its own id instead of folding it into another site
            same = next((s.site_id for s in registry.sites.values() if s.name == body.site_name), None)
            site_id = same or unique_site_id(slugify(body.site_name), registry.sites)
        else:
            site_id = slugify(Path(filename).stem.removeprefix("sensor_info_"))
        new_site = site_id not in registry.sites
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / filename
            path.write_text(body.content, encoding="utf-8")
            try:
                added = registry.import_sensor_info(path, site_id, site_name=body.site_name)
            except StorageError as exc:
                if str(path) not in str(exc):  # not about the file itself (e.g. registry.json write)
                    raise
                raise ApiFailure(422, "invalid_file", str(exc).replace(str(path), filename)) from exc
        if new_site and site_id in registry.sites:
            hub.mark_sites()
        if added:
            hub.mark_pending()
        hub.flush()
        views = [PendingView(site_id=p.site_id, alias=p.alias, address=p.address, source=p.source) for p in added]
        return _json(ImportResult(site_id=site_id, added=views))

    if sim is not None:

        def sim_device(index: int):
            if not 1 <= index <= len(sim.devices):
                raise ApiFailure(404, "not_found", f"not found: sim device {index}")
            return sim.devices[index - 1]

        @app.post("/api/sim/press/{index}")
        async def sim_press(index: int) -> Response:
            sim_device(index).press_button()
            return Response(status_code=204)

        @app.post("/api/sim/press-all")
        async def sim_press_all() -> Response:
            sim.press_all()
            return Response(status_code=204)

        @app.post("/api/sim/drop/{index}")
        async def sim_drop(index: int) -> Response:
            sim_device(index).drop_link()
            return Response(status_code=204)

    # -- WebSocket (7) --

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()  # then close with a reason code: a browser only sees 1006 for a refused handshake
        if not _origin_ok(ws):
            await ws.close(4403)
            return
        if not _token_ok(ws, token):
            await ws.close(4401)
            return
        await hub.serve(ws)

    # -- SPA and static files (8.4): whatever no route matched --

    not_found = app.router.default

    async def spa(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await not_found(scope, receive, send)
            return
        request = Request(scope, receive)
        if _is_api(request.url.path) or request.method not in ("GET", "HEAD"):
            raise StarletteHTTPException(404)
        await _spa_response(request, token, static_root)(scope, receive, send)

    app.router.default = spa
    return app


def _spa_response(request: Request, token: str, static_root: Path) -> Response:
    if "t" in request.query_params:  # first visit: swap ?t= for the cookie (4.2)
        rest = [(k, v) for k, v in request.query_params.multi_items() if k != "t"]
        # one leading "/": "//host/x" would be a scheme-relative URL to another site (open redirect)
        location = "/" + request.url.path.lstrip("/") + (f"?{urlencode(rest)}" if rest else "")
        response = RedirectResponse(location, status_code=303)
        if secrets.compare_digest(request.query_params["t"].encode(), token.encode()):
            response.set_cookie(
                cookie_name(request.headers.get("host", "")), token, path="/", httponly=True, samesite="Lax"
            )
        return response
    relative = request.url.path.lstrip("/")
    if relative:
        candidate = (static_root / relative).resolve()
        if candidate.is_relative_to(static_root) and candidate.is_file():
            response = FileResponse(candidate)
            if relative.startswith("assets/"):  # hashed names
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
    if relative.startswith("assets/"):
        raise StarletteHTTPException(404)
    index = static_root / "index.html"
    if not index.is_file():
        return PlainTextResponse(SPA_MISSING, status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-store"})
