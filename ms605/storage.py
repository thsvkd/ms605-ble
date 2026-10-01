"""ms605.storage -- on-disk persistence: atomic JSON files, the calibration
history log and per-device config snapshots.

Everything lives under ``<data_root()>/cal_results/`` (git-ignored in a source
checkout). Synchronous file I/O on purpose: the files are tiny and the core
runs on one event loop, so there is nothing to gain from threads.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platformdirs import user_data_dir

from .errors import FrameError, ProfileError, StorageError
from .models import ConfigProfile

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_FORMAT_ID = "ms605-snapshot"
_SNAPSHOT_TIME_FORMAT = "%Y%m%dT%H%M%S%fZ"


def data_root() -> Path:
    """Where `ms605` keeps its data (results live in `cal_results/` below it).
    Priority: $MS605_DATA_DIR; the repo root when running from a source
    checkout (so `cal_results/` stays next to it); else the per-user data dir
    (e.g. ~/Library/Application Support/ms605 on macOS)."""
    override = os.environ.get("MS605_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if (_REPO_ROOT / "pyproject.toml").is_file():
        return _REPO_ROOT
    return Path(user_data_dir("ms605", appauthor=False))


@dataclass(frozen=True)
class Snapshot:
    """A device's full config as it was just before a write, plus which
    sections that write was going to touch (a rollback restores only those)."""

    name: str  # file name without ".json", e.g. "20261001T120000123456Z"
    device_id: str
    taken_at: str  # ISO 8601 UTC
    reason: str  # "apply" | "rollback"
    sections: tuple[str, ...]
    profile: ConfigProfile
    dnd: bool | None = None  # tag32 before the write; only when that write changed DND


def _check_component(value: str, what: str) -> str:
    """`value` becomes a path component: refuse anything that could leave the directory."""
    if not value or Path(value).name != value or value in (".", ".."):
        raise StorageError(f"invalid {what}: {value!r}")
    return value


class Storage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else data_root() / "cal_results"
        self.registry_path = self.root / "registry.json"
        self.history_path = self.root / "calibration_history.jsonl"
        self.snapshots_dir = self.root / "snapshots"

    # -- JSON files ---------------------------------------------------------

    def read_json(self, path: Path) -> dict | None:
        """The JSON object stored at `path`, or None if the file does not exist."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise StorageError(f"cannot read {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StorageError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StorageError(f"{path} does not hold a JSON object")
        return data

    def write_json_atomic(self, path: Path, data: dict) -> None:
        """Write `data` so a crash or error leaves either the old file or the
        new one, never a partial file: temp file in the same directory, fsync, rename."""
        tmp_name: str | None = None
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
            ) as tmp:
                tmp_name = tmp.name
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        # ValueError incl. UnicodeEncodeError (a lone surrogate); TypeError: not JSON-serialisable
        except (OSError, ValueError, TypeError) as exc:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise StorageError(f"cannot write {path}: {exc}") from exc

    # -- calibration history ------------------------------------------------

    def append_history(self, record: dict) -> Path:
        """Append `record` as one JSON line to the history file (creating it as needed)."""
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise StorageError(f"cannot append to {self.history_path}: {exc}") from exc
        return self.history_path

    def read_history(self, device_id: str | None = None, *, addresses: Iterable[str] = ()) -> list[dict]:
        """History records, oldest first. With `device_id`: that device's lines,
        plus older lines written before `device_id` existed whose
        `device_address` is one of `addresses` (the sensor's cached addresses).
        Unparseable lines (e.g. a truncated last line) are skipped with a warning."""
        try:
            text = self.history_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise StorageError(f"cannot read {self.history_path}: {exc}") from exc
        known = {a.lower() for a in addresses}
        records: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                _log.warning("%s:%d: skipping unparseable history line", self.history_path, lineno)
                continue
            if device_id is not None:
                rec_id = record.get("device_id")
                if rec_id is None:
                    address = record.get("device_address")
                    if not isinstance(address, str) or address.lower() not in known:
                        continue
                elif rec_id != device_id:
                    continue
            records.append(record)
        return records

    # -- snapshots ----------------------------------------------------------

    def _snapshot_dir(self, device_id: str) -> Path:
        return self.snapshots_dir / _check_component(device_id, "device id")

    def save_snapshot(
        self, device_id: str, profile: ConfigProfile, sections: Sequence[str], reason: str, *, dnd: bool | None = None
    ) -> Snapshot:
        directory = self._snapshot_dir(device_id)
        taken = datetime.now(timezone.utc)
        while (directory / f"{taken.strftime(_SNAPSHOT_TIME_FORMAT)}.json").exists():
            taken += timedelta(microseconds=1)  # names must stay unique and ordered
        name = taken.strftime(_SNAPSHOT_TIME_FORMAT)
        data = {
            "format": _SNAPSHOT_FORMAT_ID,
            "version": 1,
            "device_id": device_id,
            "taken_at": taken.isoformat(),
            "reason": reason,
            "sections": list(sections),
            "profile": profile.to_dict(),
        }
        if dnd is not None:
            data["dnd"] = dnd
        self.write_json_atomic(directory / f"{name}.json", data)
        return Snapshot(name, device_id, data["taken_at"], reason, tuple(sections), profile, dnd)

    def load_snapshot(self, device_id: str, name: str) -> Snapshot:
        path = self._snapshot_dir(device_id) / f"{_check_component(name, 'snapshot name')}.json"
        data = self.read_json(path)
        if data is None:
            raise StorageError(f"no snapshot {name!r} for device {device_id}")
        try:
            if data["format"] != _SNAPSHOT_FORMAT_ID or data["version"] != 1:
                raise StorageError(f"{path} is not a version-1 {_SNAPSHOT_FORMAT_ID} file")
            if data["device_id"] != device_id:
                raise StorageError(f"{path} belongs to device {data['device_id']!r}")
            sections = data["sections"]
            if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
                raise StorageError(f"{path}: 'sections' must be a list of names")
            dnd = data.get("dnd")
            if dnd is not None and not isinstance(dnd, bool):
                raise StorageError(f"{path}: 'dnd' must be true or false")
            if "dnd" in sections and dnd is None:
                raise StorageError(f"{path}: the 'dnd' section has no value to restore")
            return Snapshot(
                name=name,
                device_id=device_id,
                taken_at=str(data["taken_at"]),
                reason=str(data["reason"]),
                sections=tuple(sections),
                profile=ConfigProfile.from_dict(data["profile"]),
                dnd=dnd,
            )
        except (KeyError, TypeError) as exc:
            raise StorageError(f"{path} is malformed: {exc!r}") from exc
        except (FrameError, ProfileError) as exc:
            raise StorageError(f"{path} holds an invalid profile: {exc}") from exc

    def list_snapshots(self, device_id: str) -> list[Snapshot]:
        """The device's snapshots, newest first. Unreadable ones are skipped with a warning."""
        directory = self._snapshot_dir(device_id)
        if not directory.is_dir():
            return []
        snapshots: list[Snapshot] = []
        for path in sorted(directory.glob("*.json"), reverse=True):
            try:
                snapshots.append(self.load_snapshot(device_id, path.stem))
            except StorageError as exc:
                _log.warning("skipping snapshot %s: %s", path, exc)
        return snapshots
