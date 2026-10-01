"""ms605.gui.apply -- the server's one apply job (apply, rollback or clone, one
sensor at a time), the per-sensor config_rev, config reads and the server-side
diff preview with its risk codes (docs/GUI_API.md 15.7). Every check
(404/409/422) is the route's; this module only runs requests that passed them."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, get_args

from ms605.errors import MS605ConnectionError, MS605Error, SessionBusyError, StorageError
from ms605.events import ApplyResult, ApplyStatus, CalibrationResult, Event
from ms605.fleet import Fleet, SensorChanges, ThresholdChange, apply_changes
from ms605.models import PROFILE_SECTION_KEYS, ConfigProfile, zone_distances
from ms605.protocol import DetectMode
from ms605.session import DeviceSession

from .schemas import (
    THRESHOLD_UI_MAX,
    ApplyItemState,
    ApplyItemView,
    ApplyJobView,
    ApplyKind,
    Change,
    ConfigView,
    DraftPreview,
    ProfileView,
    RiskCode,
    Section,
    SensorEdit,
    SensorPreview,
    ZonePair,
)

if TYPE_CHECKING:
    from .ws import Hub

LARGE_CHANGE = 20  # one sensitivity-preset step (protocol.SENSITIVITY_PRESETS LOW -> MEDIUM trigger, zone 0)
DND_READ_TIMEOUT_S = 3.0  # config, preview and clone-source reads: an unanswered tag32 must not stall the editor

SECTIONS: tuple[str, ...] = get_args(Section)
RISKS: tuple[str, ...] = get_args(RiskCode)
_STATE: dict[ApplyStatus, ApplyItemState] = {
    ApplyStatus.OK: "verified",
    ApplyStatus.PARTIAL: "partial",
    ApplyStatus.UNVERIFIED: "unverified",
    ApplyStatus.FAILED: "failed",
}


def _ordered(names, order: Sequence[str]) -> list:
    present = set(names)
    return [n for n in order if n in present]


# -- edits, profiles, diffs (15.7.1, 15.7.2, 15.7.4) --------------------------------------


def edit_to_changes(edit: SensorEdit) -> SensorChanges:
    zt = edit.zone_thresholds
    return SensorChanges(
        sensitivity=edit.sensitivity,
        zone_enable=None if edit.zone_enable is None else list(edit.zone_enable),
        zone_thresholds=None
        if zt is None
        else ThresholdChange(relative=zt.mode == "relative", trigger=list(zt.trigger), maintain=list(zt.maintain)),
        subsensor_zones=None if edit.subsensor_zones is None else [list(z) for z in edit.subsensor_zones],
        subsensor_timing=None if edit.subsensor_timing is None else [(p, a) for p, a in edit.subsensor_timing],
        subsensor_enable=None if edit.subsensor_enable is None else list(edit.subsensor_enable),
        dnd=edit.dnd,
    )


def edit_sections(edit: SensorEdit) -> list[str]:
    return [s for s in SECTIONS if getattr(edit, s, None) is not None]


def is_absolute(edit: SensorEdit) -> bool:
    return edit.zone_thresholds is not None and edit.zone_thresholds.mode == "absolute"


def profile_view(profile: ConfigProfile, dnd: bool | None) -> ProfileView:
    for key in PROFILE_SECTION_KEYS:
        if getattr(profile, key) is None:
            raise StorageError(f"snapshot lacks {key}")
    return ProfileView(
        sensitivity=profile.sensitivity,
        detect_mode=profile.detect_mode,
        zone_enable=list(profile.zone_enable),
        zone_thresholds=[ZonePair(trigger=t, maintain=m) for t, m in profile.zone_thresholds],
        subsensor_zones=[sorted(set(z)) for z in profile.subsensor_zones],
        subsensor_timing=[(p, a) for p, a in profile.subsensor_timing],
        subsensor_enable=list(profile.subsensor_enable),
        dnd=dnd,
    )


def _row(section: str, index: int | None, part: str, before, after) -> Change:
    return Change(section=section, index=index, part=part, before=before, after=after, risks=[])


def diff_rows(before: ProfileView, after: ProfileView) -> list[Change]:
    """One row per value that differs, in Section order (risks are filled by assess())."""
    rows: list[Change] = []
    for section in SECTIONS:
        old, new = getattr(before, section), getattr(after, section)
        if section in ("sensitivity", "detect_mode", "dnd"):
            if old != new:
                rows.append(_row(section, None, "value", old, new))
        elif section == "zone_thresholds":
            for i, (o, n) in enumerate(zip(old, new, strict=True)):
                if o.trigger != n.trigger:
                    rows.append(_row(section, i, "trigger", o.trigger, n.trigger))
                if o.maintain != n.maintain:
                    rows.append(_row(section, i, "maintain", o.maintain, n.maintain))
        elif section == "subsensor_timing":
            for i, (o, n) in enumerate(zip(old, new, strict=True)):
                if o[0] != n[0]:
                    rows.append(_row(section, i, "presence_s", o[0], n[0]))
                if o[1] != n[1]:
                    rows.append(_row(section, i, "absence_s", o[1], n[1]))
        else:  # zone_enable, subsensor_zones, subsensor_enable: one row per entry
            for i, (o, n) in enumerate(zip(old, new, strict=True)):
                if o != n:
                    rows.append(_row(section, i, "value", o, n))
    return rows


def _row_risks(row: Change, after: ProfileView, *, overwrite: bool, thresholds_changed: bool) -> set[str]:
    risks: set[str] = set()
    if row.section == "zone_thresholds":
        if overwrite:
            risks.add("absolute_overwrite")
        if abs(row.after - row.before) >= LARGE_CHANGE:
            risks.add("large_change")
        if row.after > THRESHOLD_UI_MAX:
            risks.add("beyond_ui_range")
    elif row.section == "zone_enable" and row.before and not row.after:
        risks.add("zone_off")
    elif row.section == "subsensor_enable":
        if row.before and not row.after:
            risks.add("subsensor_off")
        if row.after and not after.subsensor_zones[row.index]:
            risks.add("subsensor_no_zone")
    elif row.section == "subsensor_zones" and after.subsensor_enable[row.index] and not row.after:
        risks.add("subsensor_no_zone")
    elif row.section == "sensitivity" and not thresholds_changed:
        risks.add("sensitivity_only")
    elif row.section == "dnd" and row.after is True:
        risks.add("dnd_on")
    return risks


def assess(rows: list[Change], after: ProfileView, *, overwrite: bool, learning: bool) -> list[str]:
    """Fill each row's risks; return the sensor's (their union plus the row-less ones), RiskCode order."""
    thresholds_changed = any(r.section == "zone_thresholds" for r in rows)
    union: set[str] = {"learning_skipped"} if learning else set()
    for row in rows:
        risks = _row_risks(row, after, overwrite=overwrite, thresholds_changed=thresholds_changed)
        row.risks = _ordered(risks, RISKS)
        union |= risks
    return _ordered(union, RISKS)


def _merge(current: ConfigProfile, target: ConfigProfile) -> ConfigProfile:
    """`current` with the sections `target` writes (detect_mode 4 is skipped by the core)."""
    merged = ConfigProfile(**{k: getattr(current, k) for k in PROFILE_SECTION_KEYS})
    for key in target.sections_present():
        if key == "detect_mode" and target.detect_mode == DetectMode.SPACE_LEARNING:
            continue
        setattr(merged, key, getattr(target, key))
    return merged


# -- the job ----------------------------------------------------------------------------


@dataclass
class ApplyItem:
    device_id: str
    changes: SensorChanges | None  # apply, clone
    restore: str | None  # rollback: the snapshot to restore
    state: ApplyItemState = "queued"
    snapshot: str | None = None
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    error: str | None = None
    finished_at: float | None = None


@dataclass
class ApplyJob:
    apply_id: str
    kind: ApplyKind
    created_at: float
    source: str | None
    sections: list[str]
    items: list[ApplyItem]
    state: str = "running"


class ApplyService:
    def __init__(self, fleet: Fleet, hub: Hub) -> None:
        self.fleet = fleet
        self.hub = hub
        self.current: ApplyJob | None = None  # the running or last job
        self._rev: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    # -- queries --------------------------------------------------------------------------

    def active(self) -> bool:
        return self.current is not None and self.current.state == "running"

    def members(self) -> frozenset[str]:
        return frozenset(i.device_id for i in self.current.items) if self.active() else frozenset()

    def rev(self, device_id: str) -> int:
        return self._rev.get(device_id, 0)

    def view(self) -> ApplyJobView | None:
        job = self.current
        if job is None:
            return None
        items = [
            ApplyItemView(
                device_id=i.device_id,
                state=i.state,
                restore=i.restore,
                snapshot=i.snapshot,
                applied=i.applied,
                skipped=i.skipped,
                mismatched=i.mismatched,
                error=i.error,
                finished_at=i.finished_at,
            )
            for i in job.items
        ]
        return ApplyJobView(
            apply_id=job.apply_id,
            kind=job.kind,
            state=job.state,
            created_at=job.created_at,
            source=job.source,
            sections=job.sections,
            items=items,
        )

    # -- device reads ---------------------------------------------------------------------

    async def read_config(self, session: DeviceSession) -> ConfigView:
        rev = self.rev(session.device_id)  # before the I/O: a change meanwhile makes the apply stale
        async with session.operation("read") as ms:
            cfg = await ms.read_config()
            try:
                dnd = await ms.read_dnd(timeout=DND_READ_TIMEOUT_S)
            except MS605ConnectionError:
                raise
            except MS605Error:
                dnd = None  # this firmware does not answer tag32: the screen hides DND
        return ConfigView(
            device_id=session.device_id,
            read_at=time.time(),
            config_rev=rev,
            distances_m=list(zone_distances(cfg)),
            profile=profile_view(ConfigProfile.from_config(cfg), dnd),
        )

    async def preview(
        self,
        kind: ApplyKind,
        pairs: Sequence[tuple[str, SensorChanges]],
        *,
        several: bool,
        absolute: bool,
        learning: bool = False,
        source_rev: int | None = None,
    ) -> DraftPreview:
        overwrite = absolute and (several or kind == "clone")
        items = await asyncio.gather(
            *(self._preview_one(i, c, overwrite=overwrite, learning=learning) for i, c in pairs)
        )
        risks = _ordered({r for item in items for r in item.risks}, RISKS)
        return DraftPreview(kind=kind, checked_at=time.time(), items=list(items), risks=risks, source_rev=source_rev)

    async def _preview_one(
        self, device_id: str, changes: SensorChanges, *, overwrite: bool, learning: bool
    ) -> SensorPreview:
        rev = self.rev(device_id)
        session = self.fleet.sessions[device_id]
        try:
            async with session.operation("read") as ms:
                cfg = await ms.read_config()
                dnd = await ms.read_dnd(timeout=DND_READ_TIMEOUT_S) if changes.dnd is not None else None
            current = ConfigProfile.from_config(cfg)
            target = changes.resolve(current)
        except SessionBusyError as exc:
            return SensorPreview(
                device_id=device_id, config_rev=rev, error=f"busy: {exc.reason}", before=None, after=None,
                changes=[], risks=[],
            )  # fmt: skip
        except MS605Error as exc:  # also ProfileError (a sum outside 0..65535)
            return SensorPreview(
                device_id=device_id, config_rev=rev, error=str(exc), before=None, after=None, changes=[], risks=[]
            )
        before = profile_view(current, dnd)
        after = profile_view(_merge(current, target), dnd if changes.dnd is None else changes.dnd)
        rows = diff_rows(before, after)
        risks = assess(rows, after, overwrite=overwrite, learning=learning)
        return SensorPreview(
            device_id=device_id, config_rev=rev, error=None, before=before, after=after, changes=rows, risks=risks
        )

    # -- the job ----------------------------------------------------------------------------

    def start(
        self,
        kind: ApplyKind,
        pairs: Sequence[tuple[str, SensorChanges | str]],
        *,
        sections: Sequence[str],
        source: str | None = None,
    ) -> ApplyJobView:
        items = [
            ApplyItem(i, None, what) if isinstance(what, str) else ApplyItem(i, what, None) for i, what in pairs
        ]
        job = ApplyJob(uuid.uuid4().hex, kind, time.time(), source, _ordered(sections, SECTIONS), items)
        self.current = job
        self._task = asyncio.get_running_loop().create_task(self._run(job))
        self.hub.mark_apply()
        view = self.view()
        assert view is not None
        return view

    async def _run(self, job: ApplyJob) -> None:
        try:
            for item in job.items:
                item.state = "applying"
                self.hub.mark_apply()
                await self._run_item(item)
                item.finished_at = time.time()
                self.hub.mark_apply()
        finally:
            job.state = "done"
            self.hub.mark_apply()

    async def _run_item(self, item: ApplyItem) -> None:
        result: ApplyResult | None = None
        try:
            session = self.fleet.sessions.get(item.device_id)
            if session is None:
                item.state, item.error = "failed", "not connected"
            elif item.restore is not None:
                result = await self.fleet.rollback(item.device_id, item.restore)
            else:
                result = await apply_changes(session, item.changes, self.fleet.storage)
        except (StorageError, ValueError) as exc:  # the snapshot vanished or broke after the request
            item.state, item.error = "failed", str(exc)
        if result is not None:
            item.state = _STATE[result.status]
            item.snapshot, item.error = result.snapshot, result.error
            item.applied, item.skipped = list(result.applied), list(result.skipped)
            item.mismatched = list(result.mismatched)

    # -- events -----------------------------------------------------------------------------

    def on_event(self, ev: Event) -> None:
        """config_rev (G28): +1 for a write that got past its snapshot, or a calibration that started."""
        if ev.device_id is None:
            return
        if (isinstance(ev, ApplyResult) and ev.snapshot is not None) or (
            isinstance(ev, CalibrationResult) and ev.started
        ):
            self._rev[ev.device_id] = self.rev(ev.device_id) + 1
            self.hub.mark_sensor(ev.device_id)

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
