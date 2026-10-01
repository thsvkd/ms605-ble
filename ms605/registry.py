"""ms605.registry -- the user's sites and sensors, keyed by device id (tag30).

Every mutating method saves ``registry.json`` atomically before returning, so
callers never have to think about when to persist. Not thread-safe; one event
loop only.
"""

from __future__ import annotations

import copy
import platform
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from .errors import StorageError
from .storage import Storage

_FORMAT_ID = "ms605-registry"


@dataclass
class Site:
    site_id: str  # short slug chosen by the user, e.g. "lab-a"
    name: str


@dataclass
class Sensor:
    device_id: str  # tag30, lowercase hex; the permanent key
    site_id: str
    alias: str
    location: str = ""
    notes: str = ""
    addresses: dict[str, str] = field(default_factory=dict)  # host -> BLE address cache
    last_seen: str | None = None  # ISO 8601 UTC
    battery_pct: int | None = None


@dataclass
class PendingSensor:
    """A sensor imported from a file: known by BLE address only, until its
    device id is read on the first connect from `host`."""

    site_id: str
    alias: str
    address: str
    host: str
    source: str  # name of the imported file


@dataclass(frozen=True)
class MatchResult:
    sensor: Sensor | None
    resolved_pending: bool


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


class Registry:
    def __init__(self, storage: Storage, *, host: str | None = None) -> None:
        self._storage = storage
        self.host = host if host is not None else platform.node()
        self._sites: dict[str, Site] = {}
        self._sensors: dict[str, Sensor] = {}
        self._pending: list[PendingSensor] = []
        self._load()

    @property
    def sites(self) -> Mapping[str, Site]:
        return MappingProxyType(self._sites)

    @property
    def sensors(self) -> Mapping[str, Sensor]:
        return MappingProxyType(self._sensors)

    @property
    def pending(self) -> Sequence[PendingSensor]:
        return tuple(self._pending)

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        path = self._storage.registry_path
        data = self._storage.read_json(path)
        if data is None:
            return
        if data.get("format") != _FORMAT_ID or data.get("version") != 1:
            raise StorageError(f"{path} is not a version-1 {_FORMAT_ID} file")
        try:
            sites = {sid: Site(sid, v["name"]) for sid, v in data["sites"].items()}
            sensors = {
                did: Sensor(
                    did,
                    v["site_id"],
                    v["alias"],
                    location=v.get("location", ""),
                    notes=v.get("notes", ""),
                    addresses=dict(v.get("addresses", {})),
                    last_seen=v.get("last_seen"),
                    battery_pct=v.get("battery_pct"),
                )
                for did, v in data["sensors"].items()
            }
            pending = [PendingSensor(**p) for p in data["pending"]]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise StorageError(f"{path} is malformed: {exc!r}") from exc
        for sensor in sensors.values():
            if sensor.site_id not in sites:
                raise StorageError(f"{path}: sensor {sensor.device_id} refers to unknown site {sensor.site_id!r}")
        self._sites, self._sensors, self._pending = sites, sensors, pending

    def _save(self) -> None:
        sensors = {}
        for did, sensor in self._sensors.items():
            fields = asdict(sensor)
            del fields["device_id"]
            sensors[did] = fields
        self._storage.write_json_atomic(
            self._storage.registry_path,
            {
                "format": _FORMAT_ID,
                "version": 1,
                "sites": {sid: {"name": site.name} for sid, site in self._sites.items()},
                "sensors": sensors,
                "pending": [asdict(p) for p in self._pending],
            },
        )

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Save when the block ends; if saving fails, put the in-memory state back
        so it never claims more than the file does. Validate before entering."""
        before = copy.deepcopy((self._sites, self._sensors, self._pending))
        try:
            yield
            self._save()
        except BaseException:
            self._sites, self._sensors, self._pending = before
            raise

    # -- CRUD ---------------------------------------------------------------

    def add_site(self, site_id: str, name: str) -> Site:
        if site_id in self._sites:
            raise ValueError(f"site {site_id!r} already exists")
        with self._mutation():
            site = self._sites[site_id] = Site(site_id, name)
        return site

    def add_sensor(self, device_id: str, site_id: str, alias: str, *, location: str = "", notes: str = "") -> Sensor:
        if site_id not in self._sites:
            raise KeyError(site_id)
        if device_id in self._sensors:
            raise ValueError(f"sensor {device_id} already exists")
        with self._mutation():
            sensor = self._sensors[device_id] = Sensor(device_id, site_id, alias, location=location, notes=notes)
        return sensor

    def update_sensor(
        self,
        device_id: str,
        *,
        site_id: str | None = None,
        alias: str | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> Sensor:
        if device_id not in self._sensors:
            raise KeyError(device_id)
        if site_id is not None and site_id not in self._sites:
            raise KeyError(site_id)
        with self._mutation():
            sensor = self._sensors[device_id]
            if site_id is not None:
                sensor.site_id = site_id
            if alias is not None:
                sensor.alias = alias
            if location is not None:
                sensor.location = location
            if notes is not None:
                sensor.notes = notes
        return sensor

    def remove_sensor(self, device_id: str) -> None:
        if device_id not in self._sensors:
            raise KeyError(device_id)
        with self._mutation():
            del self._sensors[device_id]

    # -- matching and address cache -----------------------------------------

    def match(self, device_id: str, address: str, *, battery_pct: int | None = None) -> MatchResult:
        """Called right after a link is identified: refresh a registered sensor's
        cache, or turn this host's pending import for `address` into a sensor.
        An unknown device is left alone (registering it is the UI's decision)."""
        sensor = self._sensors.get(device_id)
        pending = None
        if sensor is None:
            pending = next(
                (p for p in self._pending if p.host == self.host and p.address.lower() == address.lower()), None
            )
            if pending is None:
                return MatchResult(None, False)
        with self._mutation():
            if pending is not None:
                self._pending.remove(pending)
                sensor = self._sensors[device_id] = Sensor(device_id, pending.site_id, pending.alias)
            sensor.addresses[self.host] = address
            sensor.last_seen = datetime.now(timezone.utc).isoformat()
            sensor.battery_pct = battery_pct
        return MatchResult(sensor, pending is not None)

    def address_for(self, device_id: str) -> str | None:
        """The BLE address this host last used for the sensor, if any."""
        sensor = self._sensors.get(device_id)
        return sensor.addresses.get(self.host) if sensor else None

    # -- import -------------------------------------------------------------

    def import_sensor_info(self, path: Path, site_id: str, *, site_name: str | None = None) -> list[PendingSensor]:
        """Read a `sensor_info_*.yaml` (one `name: address` per line, no YAML
        library needed) into pending sensors for this host. All or nothing; a
        re-import adds only what is new."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StorageError(f"cannot read {path}: {exc}") from exc
        entries: list[tuple[str, str]] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, address = line.partition(": ")  # MAC colons have no space after them
            name, address = name.strip(), _unquote(address.strip())
            if not sep or not name or not address:
                raise StorageError(f"{path}:{lineno}: expected 'name: address', got {line!r}")
            entries.append((name, address))

        known = {p.address.lower() for p in self._pending if p.site_id == site_id and p.host == self.host}
        known |= {s.addresses[self.host].lower() for s in self._sensors.values() if self.host in s.addresses}
        added: list[PendingSensor] = []
        for name, address in entries:
            if address.lower() in known:
                continue
            known.add(address.lower())
            added.append(PendingSensor(site_id, name, address, self.host, path.name))
        if site_id in self._sites and not added:
            return []
        with self._mutation():
            if site_id not in self._sites:
                self._sites[site_id] = Site(site_id, site_name or site_id)
            self._pending.extend(added)
        return added
