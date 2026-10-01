"""ms605.gui.server -- create_app(): token/Host/Origin checks, the REST routes,
error mapping, /ws and the built SPA (docs/GUI_API.md sections 4, 6, 8.4).
Every handler is `async def`: the core runs on this one event loop (8.3)."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.resources
import logging
import re
import secrets
import tempfile
import time
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ms605.errors import MS605ConnectionError, MS605Error, SessionBusyError, StorageError
from ms605.events import LinkState
from ms605.fleet import Fleet, SensorChanges
from ms605.models import ConfigProfile
from ms605.protocol import DetectMode
from ms605.registry import Registry
from ms605.sim import SimFleet
from ms605.storage import Storage

from .apply import DND_READ_TIMEOUT_S, SECTIONS, edit_sections, edit_to_changes, is_absolute, profile_view
from .batch import MAX_SCHEDULE_AHEAD_S
from .schemas import (
    NEW_SITE_ID_PATTERN,
    ApiError,
    ApplyIn,
    BatchCreate,
    BatchRetry,
    BatchStart,
    CalibrationHistory,
    CalibrationRecord,
    CalibrationZone,
    CloneApplyIn,
    CloneIn,
    DeviceHistory,
    DeviceHistoryKind,
    DraftIn,
    ErrorBody,
    GatherStatus,
    Health,
    ImportResult,
    LightRecordView,
    PendingView,
    PreflightRequest,
    PreflightResult,
    PresenceRecordView,
    PresenceView,
    ReleaseRequest,
    RollbackApplyIn,
    RollbackIn,
    RollbackItem,
    SensorCreate,
    SensorInfoImport,
    SensorUpdate,
    ServerInfo,
    SimInfo,
    SiteCreate,
    SiteView,
    SnapshotDetail,
    SnapshotList,
    SnapshotName,
    SnapshotView,
    StateSnapshot,
    TimeSyncIn,
    TimeSyncItem,
    TimeSyncResult,
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
    speed = sim.devices[0].speed if sim and sim.devices else 1.0
    server_info = ServerInfo(version=server_version(), lan=lan, sim=sim_info)
    hub = Hub(fleet, server_info, queue_size=ws_queue_size, speed=speed)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.attach()
        try:
            yield
        finally:
            hub.close_clients()
            try:
                await hub.live.aclose()
                await hub.batches.aclose()
                await hub.applies.aclose()
                await fleet.aclose()  # cancel an unfinished batch, stop gathering, release every link (D5)
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

    @app.exception_handler(MS605Error)  # neither a lock nor a link failure (those have their own, closer handlers)
    async def _on_device(_r: Request, exc: MS605Error) -> JSONResponse:
        if isinstance(exc, ValueError):  # ProfileError, FrameError: the MRO reaches MS605Error first
            return _error(422, "invalid", str(exc))
        return _error(502, "device_error", str(exc))

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

    # G22: a release and a batch create/retry never interleave. Otherwise a batch created while a
    # release awaits (stop_gather, connect cancels) passes its checks and then loses its links.
    operation = asyncio.Lock()

    @app.post("/api/release")
    async def release(body: ReleaseRequest) -> Response:
        async with operation:
            return await release_locked(body)

    async def release_locked(body: ReleaseRequest) -> Response:
        if body.device_ids is None:  # everything: stop gathering first, or it reconnects what is still advertising
            if hub.batches.active():  # G22: before anything changes
                raise ApiFailure(409, "batch_active", "a calibration batch is in progress")
            if hub.applies.active():  # G27
                raise ApiFailure(409, "apply_active", "a settings apply is in progress")
            await fleet.stop_gather()
            hub.clear_connecting()
            ids = list(fleet.sessions)
        else:
            ids = body.device_ids
            missing = [i for i in ids if i not in fleet.sessions]
            if missing:  # checked before anything is closed
                raise ApiFailure(404, "not_found", f"no session: {', '.join(missing)}")
            calibrating = [i for i in ids if i in hub.batches.members()]
            if calibrating:  # G22: dropping the link resets the learning
                raise ApiFailure(409, "batch_active", f"in a calibration batch: {', '.join(calibrating)}")
            applying = [i for i in ids if i in hub.applies.members()]
            if applying:  # G27: closing the link mid-write leaves the sensor half-written
                raise ApiFailure(409, "apply_active", f"in a settings apply: {', '.join(applying)}")
            if fleet.gathering:  # the button window outlasts the link: keep this gather run off them
                released.update(fleet.sessions[i].address.lower() for i in ids)
        await fleet.release(body.device_ids)
        hub.live.refresh(ids)  # release() leaves `sessions` only after the close events
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

    # -- preflight and batch calibration (14.5) --

    def need_sessions(device_ids: Sequence[str]) -> None:
        missing = [i for i in device_ids if i not in fleet.sessions]
        if missing:
            raise ApiFailure(404, "not_found", f"no session: {', '.join(missing)}")

    def check_targets(device_ids: Sequence[str]) -> None:
        """14.5.3 steps 2-4: a session each, CONNECTED, and no lock but a transient "identify"."""
        need_sessions(device_ids)
        offline = [i for i in device_ids if fleet.sessions[i].state is not LinkState.CONNECTED]
        if offline:
            raise ApiFailure(409, "not_connected", f"not connected: {', '.join(offline)}")
        locked = [i for i in device_ids if fleet.sessions[i].busy not in (None, "identify")]  # G22
        busy = [f"{i}: {fleet.sessions[i].busy}" for i in locked]
        if busy:
            raise ApiFailure(409, "busy", ", ".join(busy))
        applying = [i for i in device_ids if i in hub.applies.members()]
        if applying:  # G27
            raise ApiFailure(409, "apply_active", f"in a settings apply: {', '.join(applying)}")

    def start_of(body: BatchStart):
        if body.start == "delay":
            return float(body.delay_s)  # a human delay: not divided by the simulator speed
        if body.start == "at":
            if body.at.timestamp() - time.time() > MAX_SCHEDULE_AHEAD_S:
                raise ApiFailure(422, "invalid", "at is more than 24 h ahead")
            return body.at  # a past time: the core's ValueError -> 422 invalid
        return 0.0

    def our_batch(batch_id: str) -> None:
        if hub.batches.batch_id != batch_id:
            raise ApiFailure(404, "not_found", f"not found: batch {batch_id}")

    def no_active_batch() -> None:
        if hub.batches.active():
            raise ApiFailure(409, "batch_active", "a calibration batch is in progress")

    @app.post("/api/preflight")
    async def preflight(body: PreflightRequest) -> JSONResponse:
        need_sessions(body.device_ids)
        members = hub.batches.members()
        busy = [i for i in body.device_ids if fleet.sessions[i].busy == "calibration" or i in members]
        if busy:
            raise ApiFailure(409, "busy", f"calibrating: {', '.join(busy)}")
        snapshots = await fleet.preflight(body.device_ids, window_s=body.window_s)
        fields = ("samples", "presence", "pir", "occupied", "error")
        results = [PresenceView(device_id=i, **{f: getattr(snapshots[i], f) for f in fields}) for i in body.device_ids]
        return _json(PreflightResult(checked_at=time.time(), results=results))

    @app.post("/api/batches")
    async def create_batch(body: BatchCreate) -> JSONResponse:
        async with operation:
            no_active_batch()
            check_targets(body.device_ids)
            start = start_of(body)
            view = hub.batches.create(body.device_ids, body.start, start, presence_override=body.presence_override)
            hub.flush()
            return _json(view, 202)

    @app.get("/api/batches/{batch_id}")
    async def get_batch(batch_id: str) -> JSONResponse:
        our_batch(batch_id)
        return _json(hub.batches.view())

    @app.post("/api/batches/{batch_id}/cancel")
    async def cancel_batch(batch_id: str) -> JSONResponse:
        our_batch(batch_id)
        view = await hub.batches.cancel()
        hub.flush()
        return _json(view)

    @app.post("/api/batches/{batch_id}/retry")
    async def retry_batch(batch_id: str, body: BatchRetry) -> JSONResponse:
        async with operation:
            return retry_locked(batch_id, body)

    def retry_locked(batch_id: str, body: BatchRetry) -> JSONResponse:
        our_batch(batch_id)
        no_active_batch()
        candidates = hub.batches.retryable_ids()
        if body.device_ids is None:
            ids = [i for i in candidates if i in fleet.sessions and fleet.sessions[i].state is LinkState.CONNECTED]
            if not ids:
                raise ApiFailure(409, "not_connected", "no retryable sensor is connected")
        else:
            ids = list(dict.fromkeys(body.device_ids))
            wrong = [i for i in ids if i not in candidates]
            if wrong:
                raise ApiFailure(422, "invalid", f"not retryable: {', '.join(wrong)}")
        check_targets(ids)
        start = start_of(body)
        view = hub.batches.retry(ids, body.start, start)
        hub.flush()
        return _json(view, 202)

    # -- M4: config, drafts, apply, rollback, clone, time sync, history (15.5) --

    def not_connected(device_id: str) -> bool:
        return fleet.sessions[device_id].state is not LinkState.CONNECTED

    def check_free(device_ids: Sequence[str], *, connected: bool) -> None:
        """15.5.2: the checks shared by every M4 route that touches a device, in order."""
        need_sessions(device_ids)
        steps = (
            ("batch_active", "in a calibration batch", lambda i: i in hub.batches.members()),
            ("apply_active", "in a settings apply", lambda i: i in hub.applies.members()),
            ("not_connected", "not connected", lambda i: connected and not_connected(i)),
            ("busy", "calibrating", lambda i: fleet.sessions[i].busy == "calibration"),
        )
        for code, what, hit in steps:
            ids = [i for i in device_ids if hit(i)]
            if ids:
                raise ApiFailure(409, code, f"{what}: {', '.join(ids)}")

    def no_active_apply() -> None:
        if hub.applies.active():
            raise ApiFailure(409, "apply_active", "a settings apply is in progress")

    def check_rev(expect_rev: dict[str, int]) -> None:
        stale = [i for i, rev in expect_rev.items() if hub.applies.rev(i) != rev]
        if stale:
            raise ApiFailure(409, "stale", f"settings changed since the preview: {', '.join(stale)}")

    def known_sensor(device_id: str) -> None:
        if device_id not in registry.sensors and device_id not in fleet.sessions:
            raise ApiFailure(404, "not_found", f"not found: {device_id}")

    def draft_changes(body: DraftIn) -> SensorChanges:
        changes = edit_to_changes(body.changes)
        changes.validate()  # ProfileError -> 422 invalid
        return changes

    def load_rollback(items: Sequence[RollbackItem]) -> tuple[list[tuple[str, SensorChanges]], list[str]]:
        """Each item's snapshot as an absolute draft, and the union of their sections (Section order)."""
        pairs: list[tuple[str, SensorChanges]] = []
        sections: set[str] = set()
        for item in items:
            if not (storage.snapshots_dir / item.device_id / f"{item.snapshot}.json").is_file():
                raise ApiFailure(404, "not_found", f"no snapshot {item.snapshot} for {item.device_id}")
            snap = storage.load_snapshot(item.device_id, item.snapshot)  # StorageError -> 500
            pairs.append((item.device_id, SensorChanges.from_profile(snap.profile, snap.sections, dnd=snap.dnd)))
            sections.update(snap.sections)
        return pairs, [s for s in SECTIONS if s in sections]

    async def read_source(source: str, sections: Sequence[str]) -> tuple[SensorChanges, bool]:
        """G36: the clone source read now. (changes, the source is learning and detect_mode was picked)."""
        async with fleet.sessions[source].operation("read") as ms:
            cfg = await ms.read_config()
            dnd = await ms.read_dnd(timeout=DND_READ_TIMEOUT_S) if "dnd" in sections else None
        changes = SensorChanges.from_profile(ConfigProfile.from_config(cfg), sections, dnd=dnd)
        return changes, cfg.detect_mode == DetectMode.SPACE_LEARNING and "detect_mode" in sections

    @app.get("/api/sensors/{device_id}/config")
    async def read_config(device_id: str) -> JSONResponse:
        check_free([device_id], connected=True)
        return _json(await hub.applies.read_config(fleet.sessions[device_id]))

    @app.post("/api/drafts/preview")
    async def preview_draft(body: DraftIn) -> JSONResponse:
        changes = draft_changes(body)
        check_free(body.targets, connected=False)
        pairs = [(i, changes) for i in body.targets]
        preview = await hub.applies.preview(
            "apply", pairs, several=len(body.targets) > 1, absolute=is_absolute(body.changes)
        )
        return _json(preview)

    @app.post("/api/apply")
    async def apply_draft(body: ApplyIn) -> JSONResponse:
        changes = draft_changes(body)
        async with operation:
            no_active_apply()
            check_free(body.targets, connected=True)
            check_rev(body.expect_rev)
            pairs = [(i, changes) for i in body.targets]
            view = hub.applies.start("apply", pairs, sections=edit_sections(body.changes))
            hub.flush()
            return _json(view, 202)

    @app.get("/api/apply/{apply_id}")
    async def get_apply(apply_id: str) -> JSONResponse:
        view = hub.applies.view()
        if view is None or view.apply_id != apply_id:
            raise ApiFailure(404, "not_found", f"not found: apply {apply_id}")
        return _json(view)

    @app.get("/api/sensors/{device_id}/snapshots")
    async def list_snapshots(device_id: str) -> JSONResponse:
        known_sensor(device_id)
        snapshots = [
            SnapshotView(name=s.name, taken_at=s.taken_at, reason=s.reason, sections=list(s.sections))
            for s in storage.list_snapshots(device_id)
        ]
        return _json(SnapshotList(device_id=device_id, snapshots=snapshots))

    @app.get("/api/sensors/{device_id}/snapshots/{name}")
    async def get_snapshot(device_id: str, name: SnapshotName) -> JSONResponse:
        known_sensor(device_id)
        if not (storage.snapshots_dir / device_id / f"{name}.json").is_file():
            raise ApiFailure(404, "not_found", f"no snapshot {name} for {device_id}")
        snap = storage.load_snapshot(device_id, name)
        view = SnapshotView(name=snap.name, taken_at=snap.taken_at, reason=snap.reason, sections=list(snap.sections))
        return _json(SnapshotDetail(device_id=device_id, snapshot=view, profile=profile_view(snap.profile, snap.dnd)))

    @app.post("/api/rollback/preview")
    async def preview_rollback(body: RollbackIn) -> JSONResponse:
        check_free([i.device_id for i in body.items], connected=False)
        pairs, _ = load_rollback(body.items)
        return _json(await hub.applies.preview("rollback", pairs, several=False, absolute=False))

    @app.post("/api/rollback")
    async def rollback(body: RollbackApplyIn) -> JSONResponse:
        async with operation:
            no_active_apply()
            check_free([i.device_id for i in body.items], connected=True)
            check_rev(body.expect_rev)
            _, sections = load_rollback(body.items)
            view = hub.applies.start("rollback", [(i.device_id, i.snapshot) for i in body.items], sections=sections)
            hub.flush()
            return _json(view, 202)

    def clone_sections(body: CloneIn) -> list[str]:
        return [s for s in SECTIONS if s in body.sections]

    @app.post("/api/clone/preview")
    async def preview_clone(body: CloneIn) -> JSONResponse:
        check_free([body.source, *body.targets], connected=False)
        check_free([body.source], connected=True)
        sections = clone_sections(body)
        source_rev = hub.applies.rev(body.source)  # before the read: a change meanwhile makes the clone stale
        changes, learning = await read_source(body.source, sections)
        preview = await hub.applies.preview(
            "clone",
            [(i, changes) for i in body.targets],
            several=len(body.targets) > 1,
            absolute="zone_thresholds" in sections,
            learning=learning,
            source_rev=source_rev,
        )
        return _json(preview)

    @app.post("/api/clone")
    async def clone(body: CloneApplyIn) -> JSONResponse:
        def checks() -> None:
            no_active_apply()
            check_free([body.source, *body.targets], connected=True)
            check_rev(body.expect_rev)

        checks()  # before the read: no device I/O for a request that is refused anyway
        sections = clone_sections(body)
        source_rev = hub.applies.rev(body.source)  # before the read: a change meanwhile makes the clone stale
        changes, _ = await read_source(body.source, sections)  # outside `operation`: a slow source holds no other job
        async with operation:
            checks()  # again: anything may have started or changed during the read
            check_rev({body.source: source_rev})
            pairs = [(i, changes) for i in body.targets]
            view = hub.applies.start("clone", pairs, sections=sections, source=body.source)
            hub.flush()
            return _json(view, 202)

    @app.post("/api/time-sync")
    async def time_sync(body: TimeSyncIn) -> JSONResponse:
        items: list[TimeSyncItem] = []
        async with operation:  # only the check: a slow sensor must not hold every apply and batch behind it
            check_free(body.device_ids, connected=False)
        for device_id in body.device_ids:  # one at a time (G34); the session lock keeps a job off that sensor
            when = datetime.now(timezone.utc)
            try:
                async with fleet.sessions[device_id].operation("apply") as ms:
                    await ms.set_time(when)
            except SessionBusyError as exc:
                items.append(TimeSyncItem(device_id=device_id, written_at=None, error=f"busy: {exc.reason}"))
            except MS605Error as exc:  # e.g. "not connected"
                items.append(TimeSyncItem(device_id=device_id, written_at=None, error=str(exc)))
            else:
                items.append(TimeSyncItem(device_id=device_id, written_at=when.timestamp(), error=None))
        return _json(TimeSyncResult(items=items))

    @app.get("/api/sensors/{device_id}/history")
    async def calibration_history(device_id: str) -> JSONResponse:
        known_sensor(device_id)
        sensor, session = registry.sensors.get(device_id), fleet.sessions.get(device_id)
        addresses = list(sensor.addresses.values()) if sensor is not None else []
        if session is not None:
            addresses.append(session.address)
        records = [r for r in map(_calibration_record, storage.read_history(device_id, addresses=addresses)) if r]
        return _json(CalibrationHistory(device_id=device_id, records=records[::-1]))

    @app.get("/api/sensors/{device_id}/device-history")
    async def device_history(
        device_id: str, kind: DeviceHistoryKind = "presence", detail: bool = False
    ) -> JSONResponse:
        check_free([device_id], connected=True)
        presence: list[PresenceRecordView] = []
        light: list[LightRecordView] = []
        async with fleet.sessions[device_id].operation("read") as ms:
            if kind == "presence":
                records = await ms.read_presence_history(detail=detail)
                presence = [
                    PresenceRecordView(
                        index=r.index,
                        timestamp=r.timestamp,
                        sensor_presence=_bits(r.sensor_presence_mask, 3),
                        zone_enabled=_bits(r.zone_enable_mask, 7),
                        zone_presence=_bits(r.zone_presence_mask, 7),
                        sub_sensor_triggers=list(r.sub_sensor_triggers),
                        zone_triggers=list(r.zone_triggers),
                    )
                    for r in records
                ]
            else:
                light = [
                    LightRecordView(index=r.index, timestamp=r.timestamp, light_lux=r.light_lux)
                    for r in await ms.read_light_history()
                ]
        history = DeviceHistory(
            device_id=device_id, kind=kind, detail=detail, read_at=time.time(), presence=presence, light=light
        )
        return _json(history)


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


def _bits(mask: int, count: int) -> list[bool]:
    return [bool(mask & (1 << i)) for i in range(count)]


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _calibration_record(line: dict) -> CalibrationRecord | None:
    """15.5.9: one history line, or None (logged) when a hand-edited line lacks the required shape."""
    timestamp, zones = line.get("timestamp"), line.get("zones")
    if not isinstance(timestamp, str) or not isinstance(zones, list):
        _log.warning("skipping a calibration history line without a timestamp or zones")
        return None
    parsed: list[CalibrationZone] = []
    for zone in zones:
        if not isinstance(zone, dict) or not all(_int(zone.get(k)) for k in ("index", "trigger", "maintain")):
            _log.warning("skipping a calibration history line with a malformed zone")
            return None
        distance = zone.get("distance_m")
        if not isinstance(distance, (int, float)) or isinstance(distance, bool):
            distance = None
        parsed.append(
            CalibrationZone(
                index=zone["index"],
                distance_m=distance,
                trigger=zone["trigger"],
                maintain=zone["maintain"],
            )
        )
    name = line.get("device_name")
    return CalibrationRecord(
        timestamp=timestamp,
        device_name=name if isinstance(name, str) else None,
        sensitivity=line.get("sensitivity") if _int(line.get("sensitivity")) else None,
        detect_mode=line.get("detect_mode") if _int(line.get("detect_mode")) else None,
        zones=parsed,
    )


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
