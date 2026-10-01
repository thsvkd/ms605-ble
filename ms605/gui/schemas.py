"""ms605.gui.schemas -- the GUI wire format. These pydantic models are the single
source of truth for web/src/api/schema.ts (see docs/GUI_API.md section 5)."""

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, StringConstraints, model_validator
from pydantic.json_schema import models_json_schema

from ms605.events import BatchState, CalibrationState, LinkState

# -- field types ---------------------------------------------------------------

SiteId = Annotated[str, StringConstraints(min_length=1, max_length=64)]  # any existing id (CLI may have made it)
NEW_SITE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
NewSiteId = Annotated[str, StringConstraints(pattern=NEW_SITE_ID_PATTERN)]
DeviceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{2,128}$")]
Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
SiteName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Notes = Annotated[str, StringConstraints(max_length=2000)]


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    # fields with defaults are still always present on the wire -> non-optional in TS
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


# -- requests --------------------------------------------------------------------


class SiteCreate(In):
    name: SiteName
    site_id: NewSiteId | None = None  # None: derived from name (6.3)


class SensorCreate(In):
    device_id: DeviceId
    site_id: SiteId
    alias: Alias
    location: Location = ""
    notes: Notes = ""


class SensorUpdate(In):  # None = leave unchanged
    site_id: SiteId | None = None
    alias: Alias | None = None
    location: Location | None = None
    notes: Notes | None = None


class ReleaseRequest(In):
    device_ids: list[DeviceId] | None = None  # None = every session


class SensorInfoImport(In):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content: Annotated[str, StringConstraints(max_length=65536)]
    site_id: SiteId | None = None  # None: derived from site_name or filename (6.5)
    site_name: SiteName | None = None


# -- views -------------------------------------------------------------------------


class Health(Out):
    status: Literal["ok"] = "ok"
    version: str


class SiteView(Out):
    site_id: str
    name: str


class RegistryInfo(Out):
    site_id: str
    alias: str
    location: str
    notes: str
    last_seen: str | None  # ISO 8601 UTC
    battery_pct: int | None  # as of last_seen


class LiveInfo(Out):
    address: str  # this host's BLE address
    name: str | None  # BLE advertised name
    link: LinkState
    busy: str | None  # "identify" | "read" | "apply" | "calibration" | None
    lost_reason: str
    battery_pct: int | None  # read when identified in this run
    firmware: str | None  # DeviceInfo.version joined with "."
    light_lux: int | None
    gathered_at: float  # epoch s of the latest SensorGathered in this run


class CalibrationSummary(Out):
    timestamp: str  # ISO 8601
    sensitivity: int | None
    detect_mode: int | None


class SnapshotSummary(Out):
    name: str
    taken_at: str  # ISO 8601 UTC
    reason: str


class SensorView(Out):
    device_id: str
    registry: RegistryInfo | None  # None: not registered
    live: LiveInfo | None  # None: no session in this run (never gathered, or released)
    last_calibration: CalibrationSummary | None
    last_snapshot: SnapshotSummary | None


class PendingView(Out):
    site_id: str
    alias: str
    address: str
    source: str  # imported file name


class ConnectingDevice(Out):
    address: str
    since: float  # epoch s


class GatherStatus(Out):
    gathering: bool
    connecting: list[ConnectingDevice]  # new (not yet identified) devices being connected


class SimInfo(Out):
    count: int
    speed: float


class ServerInfo(Out):
    version: str
    lan: bool
    sim: SimInfo | None


# -- M3: live monitor ------------------------------------------------------------------


class LiveWatch(In):
    device_ids: Annotated[list[DeviceId], Field(max_length=32)]


class LiveSubscribeMessage(In):  # client -> server
    type: Literal["live_subscribe"]
    data: LiveWatch


class LiveUnsubscribeMessage(In):  # client -> server
    type: Literal["live_unsubscribe"]
    data: LiveWatch


class ClientMessage(
    RootModel[Annotated[LiveSubscribeMessage | LiveUnsubscribeMessage, Field(discriminator="type")]]
):
    pass


class LiveZone(Out):
    index: int  # 0..6
    distance_m: float  # far edge, models.zone_distances()
    enabled: bool
    trigger_active: bool  # the device's own flag (colours the trigger cur/thr text, as the CLI)
    trigger: int  # current_trigger
    trigger_threshold: int  # signed: calibration can make it zero or negative
    maintain: int  # current_maintain
    maintain_threshold: int


class LiveData(Out):
    device_id: str
    at: float  # epoch s of the tag55 push behind this frame
    pir: bool | None  # session.last_pir; None: not seen in this stream yet
    sub_sensor_presence: list[bool]  # S1..S3, the device's own presence call
    zones: list[LiveZone]


class Countdown(Out):
    batch_id: str
    fire_at: float  # epoch s (server clock)
    remaining_s: float  # fire_at - ts, never below 0


# -- M3: preflight and batch calibration -------------------------------------------------


class PreflightRequest(In):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)]
    window_s: Annotated[float, Field(ge=0.2, le=10.0)] = 3.0  # calibration.PREFLIGHT_WINDOW_S


class PresenceView(Out):
    device_id: str
    samples: int  # tag55 pushes seen in the window
    presence: bool | None  # any sub-sensor presence; None: no sample
    pir: bool | None  # PIR seen detected; None: no PIR value seen
    occupied: bool | None  # presence or pir; None: neither seen
    error: str | None  # e.g. "not connected"


class PreflightResult(Out):
    checked_at: float  # epoch s
    results: list[PresenceView]  # request order


StartMode = Literal["now", "delay", "at"]


class BatchStart(In):
    start: StartMode = "now"
    delay_s: Annotated[int, Field(ge=1, le=3600)] | None = None  # required iff start == "delay"
    at: AwareDatetime | None = None  # required iff start == "at"; ISO 8601 with an offset

    @model_validator(mode="after")
    def _check_start(self) -> "BatchStart":
        if (self.start == "delay") != (self.delay_s is not None):
            raise ValueError("delay_s is required with start='delay', and only then")
        if (self.start == "at") != (self.at is not None):
            raise ValueError("at is required with start='at', and only then")
        return self


class BatchCreate(BatchStart):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)]
    presence_override: bool = False  # the operator started despite an occupied preflight

    @model_validator(mode="after")
    def _check_ids(self) -> "BatchCreate":
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("device_ids has duplicates")
        return self


class BatchRetry(BatchStart):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)] | None = None  # None: every retryable


class ZonePair(Out):
    trigger: int
    maintain: int


class CalibrationJobView(Out):
    batch_id: str
    device_id: str
    attempt: int  # 1, +1 each time a retry includes this sensor
    state: CalibrationState  # "idle": waiting for the fire time
    started: bool  # tag52=4 was sent (lost + started: dropped while learning)
    elapsed_s: float | None  # since LEARNING began, last CalibrationProgress; None before LEARNING
    error: str | None
    detail: str
    before: list[ZonePair] | None  # tag51 just before STARTING
    after: list[ZonePair] | None  # tag51 read back after SUCCEEDED
    history_saved: bool
    retryable: bool  # state is failed, lost or timeout


class BatchView(Out):
    batch_id: str  # stable across retries (the first round's core batch id)
    state: BatchState  # of the current round
    round: int  # 1, +1 per retry
    start: StartMode  # of the current round
    fire_at: float  # epoch s, current round
    created_at: float  # epoch s, first round
    expected_s: float  # progress denominator: EXPECTED_CALIBRATION_S / sim speed (not device progress)
    presence_override: bool
    device_ids: list[str]  # every sensor of the batch, selection order
    round_ids: list[str]  # the sensors of the current round
    jobs: list[CalibrationJobView]  # device_ids order


class StateSnapshot(Out):
    seq: int
    server: ServerInfo
    gather: GatherStatus
    sites: list[SiteView]
    sensors: list[SensorView]
    pending: list[PendingView]
    batch: BatchView | None  # the current or last batch, kept until the next one (late joiners)


class ImportResult(Out):
    site_id: str
    added: list[PendingView]


ErrorCode = Literal[
    "unauthorized", "forbidden_origin", "not_found", "already_exists", "busy",
    "not_connected", "invalid", "invalid_request", "invalid_file", "storage", "internal", "batch_active",
]


class ErrorBody(Out):
    code: ErrorCode
    message: str


class ApiError(Out):
    error: ErrorBody


# -- websocket ---------------------------------------------------------------------


class Notice(Out):
    level: Literal["info", "warning", "error"]
    code: Literal["gather_failed", "internal"]
    message: str
    device_id: str | None
    address: str | None
    name: str | None
    at: float  # epoch s


class SensorRemoved(Out):
    device_id: str


class SitesData(Out):
    sites: list[SiteView]


class PendingData(Out):
    pending: list[PendingView]


class SnapshotMessage(Out):
    type: Literal["snapshot"] = "snapshot"
    seq: int
    ts: float
    data: StateSnapshot


class SensorMessage(Out):
    type: Literal["sensor"] = "sensor"
    seq: int
    ts: float
    data: SensorView


class SensorRemovedMessage(Out):
    type: Literal["sensor_removed"] = "sensor_removed"
    seq: int
    ts: float
    data: SensorRemoved


class SitesMessage(Out):
    type: Literal["sites"] = "sites"
    seq: int
    ts: float
    data: SitesData


class PendingMessage(Out):
    type: Literal["pending"] = "pending"
    seq: int
    ts: float
    data: PendingData


class GatherMessage(Out):
    type: Literal["gather"] = "gather"
    seq: int
    ts: float
    data: GatherStatus


class NoticeMessage(Out):
    type: Literal["notice"] = "notice"
    seq: int
    ts: float
    data: Notice


class BatchMessage(Out):
    type: Literal["batch"] = "batch"
    seq: int
    ts: float
    data: BatchView


class CalibrationJobMessage(Out):
    type: Literal["calibration_job"] = "calibration_job"
    seq: int
    ts: float
    data: CalibrationJobView


class LiveMessage(Out):  # transient: no seq, only to subscribers, coalesced per sensor
    type: Literal["live"] = "live"
    seq: None = None
    ts: float
    data: LiveData


class CountdownMessage(Out):  # transient: no seq, 1 Hz while a batch waits
    type: Literal["countdown"] = "countdown"
    seq: None = None
    ts: float
    data: Countdown


class ServerMessage(
    RootModel[
        Annotated[
            SnapshotMessage | SensorMessage | SensorRemovedMessage | SitesMessage | PendingMessage
            | GatherMessage | NoticeMessage | BatchMessage | CalibrationJobMessage | LiveMessage | CountdownMessage,
            Field(discriminator="type"),
        ]
    ]
):
    pass


REQUEST_MODELS = (
    SiteCreate, SensorCreate, SensorUpdate, ReleaseRequest, SensorInfoImport,
    PreflightRequest, BatchCreate, BatchRetry, ClientMessage,
)
RESPONSE_MODELS = (
    Health, StateSnapshot, SiteView, SensorView, ImportResult, ApiError, ServerMessage,
    PreflightResult, BatchView,
)


def openapi_document() -> dict:
    """A paths-less OpenAPI 3.1 document holding every wire model (input for openapi-typescript)."""
    _, top = models_json_schema(
        [(m, "validation") for m in REQUEST_MODELS] + [(m, "serialization") for m in RESPONSE_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    return {
        "openapi": "3.1.0",
        "info": {"title": "ms605 gui", "version": "1"},
        "paths": {},
        "components": {"schemas": top.get("$defs", {})},
    }


if __name__ == "__main__":
    text = json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    Path(sys.argv[1]).write_text(text, encoding="utf-8")
