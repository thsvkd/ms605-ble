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
    config_rev: int  # +1 each time this server run may have changed the sensor's settings (G28)


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


# -- M4: config, drafts, apply, rollback, clone, history ----------------------------------

THRESHOLD_UI_MAX = 500  # ms605/cli/cli.py THRESHOLD_MAX: the app's threshold axis (SPEC 8.3: known-safe UI range)

Section = Literal[
    "sensitivity", "detect_mode", "zone_enable", "zone_thresholds",
    "subsensor_zones", "subsensor_timing", "subsensor_enable", "dnd",
]  # models.PROFILE_SECTION_KEYS + "dnd" (CORE_API 2.3)
RiskCode = Literal[
    "absolute_overwrite", "large_change", "beyond_ui_range", "zone_off", "subsensor_off",
    "subsensor_no_zone", "sensitivity_only", "dnd_on", "learning_skipped",
]  # this order is the display order
ApplyKind = Literal["apply", "rollback", "clone"]
ApplyItemState = Literal["queued", "applying", "verified", "partial", "unverified", "failed"]
# strict: JSON true is not the integer 1, and 1 is not true (the core refuses bools as numbers too)
Flag = Annotated[bool, Field(strict=True)]
U16 = Annotated[int, Field(strict=True, ge=0, le=65535)]
ZoneIndex = Annotated[int, Field(strict=True, ge=0, le=6)]
# delta (relative: -500..500) or absolute (0..65535; a calibrated value may sit above 500, and
# beyond_ui_range flags it): ThresholdEdit checks the per-mode range
ThresholdValue = Annotated[int, Field(strict=True, ge=-THRESHOLD_UI_MAX, le=65535)]
Thresholds7 = Annotated[list[ThresholdValue | None], Field(min_length=7, max_length=7)]
DeviceIds = Annotated[list[DeviceId], Field(min_length=1, max_length=32)]
ZoneList = Annotated[list[ZoneIndex], Field(max_length=7)]
SnapshotName = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}T[0-9]{12}Z$")]  # storage: %Y%m%dT%H%M%S%fZ


def _unique(ids: list[str], what: str) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError(f"{what} has duplicates")


def _covers(expect_rev: dict[str, int], ids: list[str]) -> None:
    missing = [i for i in ids if i not in expect_rev]
    if missing:
        raise ValueError(f"expect_rev lacks {', '.join(missing)}")


class ProfileView(Out):  # core ConfigProfile, every section present, + DND
    sensitivity: int  # tag61: 1 LOW, 2 MEDIUM, 3 HIGH, 4 CUSTOM
    detect_mode: int  # tag52: 1..3; 4 = space learning
    zone_enable: list[bool]  # 7, tag50
    zone_thresholds: list[ZonePair]  # 7, tag51
    subsensor_zones: list[list[int]]  # S1..S3 zone indices, sorted, tag48
    subsensor_timing: list[tuple[int, int]]  # S1..S3 (presence_s, absence_s), tag49
    subsensor_enable: list[bool]  # S1..S3, tag41
    dnd: bool | None  # tag32; None: not read, or the device did not answer


class ConfigView(Out):
    device_id: str
    read_at: float  # epoch s
    config_rev: int  # SensorView.config_rev at the read
    distances_m: list[float]  # 7 far edges, models.zone_distances(cfg)
    profile: ProfileView


class ThresholdEdit(In):
    mode: Literal["relative", "absolute"]  # relative: current + value (D8 default for bulk)
    trigger: Thresholds7  # per zone; None leaves that zone as it is
    maintain: Thresholds7

    @model_validator(mode="after")
    def _check(self) -> "ThresholdEdit":
        values = [v for v in self.trigger + self.maintain if v is not None]
        if not values:
            raise ValueError("zone_thresholds changes no zone")
        if self.mode == "absolute" and min(values) < 0:
            raise ValueError("absolute thresholds must be >= 0")
        if self.mode == "relative" and max(values) > THRESHOLD_UI_MAX:
            raise ValueError(f"relative thresholds must be within -{THRESHOLD_UI_MAX}..{THRESHOLD_UI_MAX}")
        return self


class SensorEdit(In):  # core SensorChanges without detect_mode; None leaves the section as it is
    sensitivity: Annotated[int, Field(strict=True, ge=1, le=4)] | None = None
    zone_enable: Annotated[list[Flag], Field(min_length=7, max_length=7)] | None = None
    zone_thresholds: ThresholdEdit | None = None
    subsensor_zones: Annotated[list[ZoneList], Field(min_length=3, max_length=3)] | None = None
    subsensor_timing: Annotated[list[tuple[U16, U16]], Field(min_length=3, max_length=3)] | None = None
    subsensor_enable: Annotated[list[Flag], Field(min_length=3, max_length=3)] | None = None
    dnd: Flag | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "SensorEdit":
        if all(getattr(self, name) is None for name in type(self).model_fields):
            raise ValueError("nothing to change")
        return self


class DraftIn(In):
    targets: DeviceIds  # apply order
    changes: SensorEdit  # the same edit for every target; relative thresholds resolve per sensor
    expect_rev: dict[DeviceId, int] | None = None  # not read by the preview

    @model_validator(mode="after")
    def _check_targets(self) -> "DraftIn":
        _unique(self.targets, "targets")
        return self


class ApplyIn(DraftIn):
    expect_rev: dict[DeviceId, int]  # config_rev of every target from the preview: 409 stale if one moved

    @model_validator(mode="after")
    def _check_rev(self) -> "ApplyIn":
        _covers(self.expect_rev, self.targets)
        return self


class RollbackItem(In):
    device_id: DeviceId
    snapshot: SnapshotName


class RollbackIn(In):
    items: Annotated[list[RollbackItem], Field(min_length=1, max_length=32)]
    expect_rev: dict[DeviceId, int] | None = None  # not read by the preview

    @model_validator(mode="after")
    def _check_items(self) -> "RollbackIn":
        _unique([i.device_id for i in self.items], "items")
        return self


class RollbackApplyIn(RollbackIn):
    expect_rev: dict[DeviceId, int]  # every item's sensor

    @model_validator(mode="after")
    def _check_rev(self) -> "RollbackApplyIn":
        _covers(self.expect_rev, [i.device_id for i in self.items])
        return self


class CloneIn(In):
    source: DeviceId
    targets: DeviceIds
    sections: Annotated[list[Section], Field(min_length=1, max_length=8)]
    expect_rev: dict[DeviceId, int] | None = None  # not read by the preview

    @model_validator(mode="after")
    def _check_clone(self) -> "CloneIn":
        _unique(self.targets, "targets")
        _unique(self.sections, "sections")
        if self.source in self.targets:
            raise ValueError("source is one of the targets")
        return self


class CloneApplyIn(CloneIn):
    expect_rev: dict[DeviceId, int]  # every target, and the source as the preview's source_rev

    @model_validator(mode="after")
    def _check_rev(self) -> "CloneApplyIn":
        _covers(self.expect_rev, [*self.targets, self.source])
        return self


class Change(Out):
    section: Section
    index: int | None  # zone 0..6 or sub-sensor 0..2; None for sensitivity, detect_mode, dnd
    part: Literal["value", "trigger", "maintain", "presence_s", "absence_s"]
    before: bool | int | list[int] | None  # None: unknown (DND not readable)
    after: bool | int | list[int] | None
    risks: list[RiskCode]  # RiskCode order; empty: not risky


class SensorPreview(Out):
    device_id: str
    config_rev: int  # send back in expect_rev
    error: str | None  # read or resolve failed (e.g. "busy: read", a sum outside 0..65535): applying fails too
    before: ProfileView | None
    after: ProfileView | None  # before + the changes; what the core skips (detect_mode 4) stays as before
    changes: list[Change]  # empty: nothing would change on this sensor
    risks: list[RiskCode]  # union of the rows' risks and the sensor-wide ones, RiskCode order


class DraftPreview(Out):
    kind: ApplyKind
    checked_at: float  # epoch s
    items: list[SensorPreview]  # target order
    risks: list[RiskCode]  # union over the items, RiskCode order
    source_rev: int | None  # clone: the source's config_rev before it was read; send it back in expect_rev


class ApplyItemView(Out):
    device_id: str
    state: ApplyItemState
    restore: str | None  # rollback: the snapshot being restored
    snapshot: str | None  # the automatic pre-apply snapshot (the rollback point); None: not reached
    applied: list[Section]
    skipped: list[Section]  # e.g. detect_mode 4 in a clone
    mismatched: list[Section]  # still different after the polled verify, or after a failed write
    error: str | None
    finished_at: float | None  # epoch s


class ApplyJobView(Out):
    apply_id: str  # uuid4 hex
    kind: ApplyKind
    state: Literal["running", "done"]
    created_at: float  # epoch s
    source: str | None  # clone: the source sensor
    sections: list[Section]  # what the job writes, Section order
    items: list[ApplyItemView]  # target order, applied one at a time


class TimeSyncIn(In):
    device_ids: DeviceIds

    @model_validator(mode="after")
    def _check_ids(self) -> "TimeSyncIn":
        _unique(self.device_ids, "device_ids")
        return self


class TimeSyncItem(Out):
    device_id: str
    written_at: float | None  # epoch s written to tag33 (as int); None: failed
    error: str | None


class TimeSyncResult(Out):
    items: list[TimeSyncItem]  # request order


class CalibrationZone(Out):
    index: int
    distance_m: float | None
    trigger: int
    maintain: int


class CalibrationRecord(Out):
    timestamp: str  # ISO 8601
    device_name: str | None
    sensitivity: int | None
    detect_mode: int | None
    zones: list[CalibrationZone]


class CalibrationHistory(Out):
    device_id: str
    records: list[CalibrationRecord]  # newest first


class SnapshotView(Out):
    name: str
    taken_at: str  # ISO 8601 UTC
    reason: str  # "apply" | "rollback": the state just before that write
    sections: list[str]  # what that write changed; a rollback writes these back


class SnapshotList(Out):
    device_id: str
    snapshots: list[SnapshotView]  # newest first


class SnapshotDetail(Out):
    device_id: str
    snapshot: SnapshotView
    profile: ProfileView  # the whole configuration before that write


DeviceHistoryKind = Literal["presence", "light"]


class PresenceRecordView(Out):
    index: int
    timestamp: int  # epoch s by the sensor's clock (sync it first)
    sensor_presence: list[bool]  # S1..S3
    zone_enabled: list[bool]  # 7
    zone_presence: list[bool]  # 7
    sub_sensor_triggers: list[int]  # detail records only, else []
    zone_triggers: list[int]  # detail records only, else []


class LightRecordView(Out):
    index: int
    timestamp: int  # epoch s by the sensor's clock
    light_lux: int


class DeviceHistory(Out):  # EXPERIMENTAL (SPEC 8.8): one round trip, no pagination, layout unverified
    device_id: str
    kind: DeviceHistoryKind
    detail: bool  # presence records decoded as 37-byte detail records
    read_at: float  # epoch s
    presence: list[PresenceRecordView]  # kind == "presence"
    light: list[LightRecordView]  # kind == "light"


class StateSnapshot(Out):
    seq: int
    server: ServerInfo
    gather: GatherStatus
    sites: list[SiteView]
    sensors: list[SensorView]
    pending: list[PendingView]
    batch: BatchView | None  # the current or last batch, kept until the next one (late joiners)
    apply: ApplyJobView | None  # the running or last apply job, kept until the next one (G25)


class ImportResult(Out):
    site_id: str
    added: list[PendingView]


ErrorCode = Literal[
    "unauthorized", "forbidden_origin", "not_found", "already_exists", "busy",
    "not_connected", "invalid", "invalid_request", "invalid_file", "storage", "internal", "batch_active",
    "apply_active", "stale", "device_error",
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


class ApplyMessage(Out):
    type: Literal["apply"] = "apply"
    seq: int
    ts: float
    data: ApplyJobView


class ServerMessage(
    RootModel[
        Annotated[
            SnapshotMessage | SensorMessage | SensorRemovedMessage | SitesMessage | PendingMessage
            | GatherMessage | NoticeMessage | BatchMessage | CalibrationJobMessage | LiveMessage | CountdownMessage
            | ApplyMessage,
            Field(discriminator="type"),
        ]
    ]
):
    pass


REQUEST_MODELS = (
    SiteCreate, SensorCreate, SensorUpdate, ReleaseRequest, SensorInfoImport,
    PreflightRequest, BatchCreate, BatchRetry, ClientMessage,
    DraftIn, ApplyIn, RollbackIn, RollbackApplyIn, CloneIn, CloneApplyIn, TimeSyncIn,
)
RESPONSE_MODELS = (
    Health, StateSnapshot, SiteView, SensorView, ImportResult, ApiError, ServerMessage,
    PreflightResult, BatchView,
    ConfigView, DraftPreview, ApplyJobView, TimeSyncResult, CalibrationHistory, SnapshotList, SnapshotDetail,
    DeviceHistory,
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
