"""ms605.registry: sites/sensors CRUD with save-on-every-change, the per-host
address cache, matching a freshly identified link (including pending imports
resolving on first connect, driven through the simulator) and the
sensor_info yaml import. Storage is always under tmp_path. Every value here is
synthetic."""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import datetime

import pytest

from ms605 import MS605
from ms605.errors import StorageError
from ms605.protocol import TAG_BATTERY, TAG_DEVICE_ID
from ms605.registry import MatchResult, PendingSensor, Registry, Sensor, Site
from ms605.sim import SimMS605
from ms605.storage import Storage

ID1 = "00" * 19 + "01"
ID2 = "00" * 19 + "02"
ADDR1 = "00000000-0000-4000-8000-000000000001"
ADDR2 = "00000000-0000-4000-8000-000000000002"
MAC = "02:00:00:00:00:03"


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "data")


@pytest.fixture
def reg(storage):
    return Registry(storage, host="host-1")


def _file(storage: Storage) -> dict:
    return json.loads(storage.registry_path.read_text(encoding="utf-8"))


def _write_info(tmp_path, text: str, name: str = "sensor_info_example.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- loading ------------------------------------------------------------------


def test_missing_file_is_an_empty_registry_and_is_not_created(storage):
    reg = Registry(storage, host="host-1")
    assert (dict(reg.sites), dict(reg.sensors), list(reg.pending)) == ({}, {}, [])
    assert not storage.registry_path.exists()


def test_host_defaults_to_platform_node(storage):
    assert Registry(storage).host == platform.node()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"format": "other", "version": 1, "sites": {}, "sensors": {}, "pending": []}),
        json.dumps({"format": "ms605-registry", "version": 2, "sites": {}, "sensors": {}, "pending": []}),
        json.dumps({"format": "ms605-registry", "version": 1, "sites": {}, "sensors": {}}),
        json.dumps({"format": "ms605-registry", "version": 1, "sites": {"a": {}}, "sensors": {}, "pending": []}),
        json.dumps({"format": "ms605-registry", "version": 1, "sites": [], "sensors": {}, "pending": []}),
        json.dumps({"format": "ms605-registry", "version": 1, "sites": {}, "sensors": {}, "pending": [{"alias": "x"}]}),
        json.dumps(
            {
                "format": "ms605-registry",
                "version": 1,
                "sites": {},
                "sensors": {ID1: {"site_id": "gone", "alias": "S"}},
                "pending": [],
            }
        ),
    ],
    ids=[
        "bad-json",
        "not-object",
        "format",
        "version",
        "no-pending",
        "site-no-name",
        "sites-type",
        "pending-keys",
        "orphan",
    ],
)
def test_malformed_registry_file_raises_storage_error(storage, content):
    storage.registry_path.parent.mkdir(parents=True)
    storage.registry_path.write_text(content, encoding="utf-8")
    with pytest.raises(StorageError):
        Registry(storage, host="host-1")


def test_loads_the_documented_file_format(storage):
    storage.write_json_atomic(
        storage.registry_path,
        {
            "format": "ms605-registry",
            "version": 1,
            "sites": {"lab-a": {"name": "Lab A"}},
            "sensors": {
                ID1: {
                    "site_id": "lab-a",
                    "alias": "Sensor 1",
                    "location": "north wall",
                    "notes": "",
                    "addresses": {"host-1": ADDR1},
                    "last_seen": "2026-10-01T12:00:00+00:00",
                    "battery_pct": 87,
                }
            },
            "pending": [
                {"site_id": "lab-a", "alias": "Sensor 2", "address": ADDR2, "host": "host-1", "source": "x.yaml"}
            ],
        },
    )
    reg = Registry(storage, host="host-1")
    assert reg.sites["lab-a"] == Site("lab-a", "Lab A")
    assert reg.sensors[ID1] == Sensor(
        ID1, "lab-a", "Sensor 1", "north wall", "", {"host-1": ADDR1}, "2026-10-01T12:00:00+00:00", 87
    )
    assert reg.pending == (PendingSensor("lab-a", "Sensor 2", ADDR2, "host-1", "x.yaml"),)
    assert reg.address_for(ID1) == ADDR1


# -- CRUD -----------------------------------------------------------------------


def test_views_are_read_only(reg):
    reg.add_site("lab-a", "Lab A")
    with pytest.raises(TypeError):
        reg.sites["x"] = Site("x", "X")  # type: ignore[index]
    with pytest.raises(TypeError):
        reg.sensors["x"] = None  # type: ignore[index]
    assert not hasattr(reg.pending, "append")


def test_add_site_and_sensor_persist_immediately(reg, storage):
    site = reg.add_site("lab-a", "Lab A")
    assert site == Site("lab-a", "Lab A")
    assert _file(storage)["sites"] == {"lab-a": {"name": "Lab A"}}

    sensor = reg.add_sensor(ID1, "lab-a", "Sensor 1", location="north wall", notes="n")
    assert sensor == Sensor(ID1, "lab-a", "Sensor 1", "north wall", "n")
    assert reg.sensors[ID1] is sensor
    on_disk = _file(storage)
    assert on_disk["format"] == "ms605-registry" and on_disk["version"] == 1
    assert on_disk["sensors"][ID1] == {
        "site_id": "lab-a",
        "alias": "Sensor 1",
        "location": "north wall",
        "notes": "n",
        "addresses": {},
        "last_seen": None,
        "battery_pct": None,
    }
    assert on_disk["pending"] == []


def test_state_survives_reload(reg, storage):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")
    reg.match(ID1, ADDR1, battery_pct=50)
    again = Registry(storage, host="host-1")
    assert dict(again.sites) == dict(reg.sites)
    assert dict(again.sensors) == dict(reg.sensors)


def test_add_errors(reg):
    reg.add_site("lab-a", "Lab A")
    with pytest.raises(ValueError):
        reg.add_site("lab-a", "Again")
    with pytest.raises(KeyError):
        reg.add_sensor(ID1, "nowhere", "S")
    reg.add_sensor(ID1, "lab-a", "S")
    with pytest.raises(ValueError):
        reg.add_sensor(ID1, "lab-a", "Dup")
    assert reg.sensors[ID1].alias == "S"


def test_update_sensor_changes_only_given_fields_and_saves(reg, storage):
    reg.add_site("lab-a", "Lab A")
    reg.add_site("lab-b", "Lab B")
    reg.add_sensor(ID1, "lab-a", "Sensor 1", location="north wall", notes="n")

    updated = reg.update_sensor(ID1, alias="Renamed")
    assert (updated.alias, updated.site_id, updated.location, updated.notes) == ("Renamed", "lab-a", "north wall", "n")
    reg.update_sensor(ID1, site_id="lab-b", location="", notes="moved")
    stored = _file(storage)["sensors"][ID1]
    assert (stored["alias"], stored["site_id"], stored["location"], stored["notes"]) == (
        "Renamed",
        "lab-b",
        "",
        "moved",
    )


def test_update_sensor_errors_change_nothing(reg, storage):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")
    before = _file(storage)
    with pytest.raises(KeyError):
        reg.update_sensor(ID2, alias="x")
    with pytest.raises(KeyError):
        reg.update_sensor(ID1, site_id="nowhere", alias="x")
    assert reg.sensors[ID1].alias == "Sensor 1"
    assert _file(storage) == before


def test_remove_sensor(reg, storage):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")
    reg.remove_sensor(ID1)
    assert ID1 not in reg.sensors
    assert _file(storage)["sensors"] == {}
    with pytest.raises(KeyError):
        reg.remove_sensor(ID1)


def test_failed_save_leaves_memory_matching_the_file(reg, storage, monkeypatch):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")

    def boom(path, data):
        raise StorageError("disk full")

    monkeypatch.setattr(storage, "write_json_atomic", boom)
    with pytest.raises(StorageError):
        reg.add_site("lab-b", "Lab B")
    with pytest.raises(StorageError):
        reg.update_sensor(ID1, alias="Changed")
    with pytest.raises(StorageError):
        reg.remove_sensor(ID1)
    with pytest.raises(StorageError):
        reg.match(ID1, ADDR1, battery_pct=10)
    assert list(reg.sites) == ["lab-a"]
    assert reg.sensors[ID1].alias == "Sensor 1"
    assert reg.sensors[ID1].addresses == {}


# -- match and the address cache --------------------------------------------------


def test_match_registered_sensor_updates_cache_and_saves(reg, storage):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")
    result = reg.match(ID1, ADDR1, battery_pct=87)

    assert result == MatchResult(reg.sensors[ID1], False)
    sensor = reg.sensors[ID1]
    assert sensor.addresses == {"host-1": ADDR1}
    assert sensor.battery_pct == 87
    assert datetime.fromisoformat(sensor.last_seen).utcoffset().total_seconds() == 0
    assert _file(storage)["sensors"][ID1]["addresses"] == {"host-1": ADDR1}
    assert reg.address_for(ID1) == ADDR1

    reg.match(ID1, ADDR2)  # a new address for the same host replaces the old one
    assert reg.address_for(ID1) == ADDR2
    assert reg.sensors[ID1].battery_pct is None  # unknown this time, so not carried over


def test_match_unknown_device_returns_none_and_saves_nothing(reg, storage):
    assert reg.match(ID1, ADDR1, battery_pct=50) == MatchResult(None, False)
    assert ID1 not in reg.sensors
    assert not storage.registry_path.exists()
    assert reg.address_for(ID1) is None


def test_address_cache_is_per_host(storage):
    host1 = Registry(storage, host="host-1")
    host1.add_site("lab-a", "Lab A")
    host1.add_sensor(ID1, "lab-a", "Sensor 1")
    host1.match(ID1, ADDR1)

    host2 = Registry(storage, host="host-2")
    assert host2.address_for(ID1) is None
    host2.match(ID1, ADDR2)
    assert host2.address_for(ID1) == ADDR2

    reloaded = Registry(storage, host="host-1")
    assert reloaded.address_for(ID1) == ADDR1  # host-2's write did not clobber host-1's entry
    assert reloaded.sensors[ID1].addresses == {"host-1": ADDR1, "host-2": ADDR2}


def test_pending_resolves_on_first_match_case_insensitively(tmp_path, storage):
    reg = Registry(storage, host="host-1")
    reg.import_sensor_info(_write_info(tmp_path, f"Sensor 3: {MAC}\n"), "lab-a", site_name="Lab A")
    assert len(reg.pending) == 1

    result = reg.match(ID1, MAC.lower(), battery_pct=70)

    assert result.resolved_pending is True
    sensor = result.sensor
    assert sensor == reg.sensors[ID1]
    assert (sensor.site_id, sensor.alias, sensor.addresses, sensor.battery_pct) == (
        "lab-a",
        "Sensor 3",
        {"host-1": MAC.lower()},
        70,
    )
    assert reg.pending == ()
    assert _file(storage)["pending"] == []
    assert ID1 in _file(storage)["sensors"]
    # the second connect is an ordinary match
    assert reg.match(ID1, MAC.lower()).resolved_pending is False


def test_pending_for_another_host_or_address_is_not_resolved(tmp_path, storage):
    Registry(storage, host="host-2").import_sensor_info(_write_info(tmp_path, f"S: {MAC}\n"), "lab-a")
    reg = Registry(storage, host="host-1")
    assert len(reg.pending) == 1
    assert reg.match(ID1, MAC) == MatchResult(None, False)
    assert reg.match(ID1, ADDR1) == MatchResult(None, False)
    assert len(reg.pending) == 1 and ID1 not in reg.sensors


def test_registered_sensor_match_leaves_pending_alone(tmp_path, reg):
    reg.import_sensor_info(_write_info(tmp_path, f"S: {MAC}\n"), "lab-a")
    reg.add_sensor(ID2, "lab-a", "Other")
    assert reg.match(ID2, ADDR2).resolved_pending is False
    assert len(reg.pending) == 1


def test_pending_resolves_on_first_connect_to_simulated_device(tmp_path, storage):
    """The real flow: import the file, connect, read tag30, match."""
    dev = SimMS605(3)  # synthetic address 02:00:00:00:00:03, id b"SIM605\x00\x03"
    reg = Registry(storage, host="host-1")
    reg.import_sensor_info(_write_info(tmp_path, f"Sensor 3: {dev.address}\n"), "lab-a", site_name="Lab A")

    async def connect_and_identify():
        dev.press_button()
        ms = MS605(dev.address, client_factory=dev.client_factory, inter_chunk_delay=0)
        await ms.connect()
        try:
            frame = await ms.read_raw([TAG_DEVICE_ID, TAG_BATTERY])
        finally:
            await ms.disconnect()
        return frame.get(TAG_DEVICE_ID).hex(), frame.get(TAG_BATTERY)[0]

    device_id, battery = asyncio.run(connect_and_identify())
    result = reg.match(device_id, dev.address, battery_pct=battery)

    assert result.resolved_pending is True
    assert result.sensor.device_id == device_id == dev.tags[TAG_DEVICE_ID].hex()
    assert (result.sensor.alias, result.sensor.site_id, result.sensor.battery_pct) == ("Sensor 3", "lab-a", 87)
    assert Registry(storage, host="host-1").address_for(device_id) == dev.address


# -- yaml import ----------------------------------------------------------------


def test_import_parses_comments_blank_lines_quotes_and_mac_addresses(tmp_path, reg, storage):
    path = _write_info(
        tmp_path,
        "# exported by a tool\n"
        "\n"
        f"  Sensor 1:   {ADDR1}  \n"
        f"Sensor 2: '{ADDR2}'\n"
        f'Sensor 3: "{MAC}"\n'
        "   # indented comment\n"
        'Sensor 4: "A: b"\n',
    )
    added = reg.import_sensor_info(path, "lab-a", site_name="Lab A")

    assert added == [
        PendingSensor("lab-a", "Sensor 1", ADDR1, "host-1", "sensor_info_example.yaml"),
        PendingSensor("lab-a", "Sensor 2", ADDR2, "host-1", "sensor_info_example.yaml"),
        PendingSensor("lab-a", "Sensor 3", MAC, "host-1", "sensor_info_example.yaml"),
        PendingSensor("lab-a", "Sensor 4", "A: b", "host-1", "sensor_info_example.yaml"),
    ]
    assert list(reg.pending) == added
    assert _file(storage)["pending"][2] == {
        "site_id": "lab-a",
        "alias": "Sensor 3",
        "address": MAC,
        "host": "host-1",
        "source": "sensor_info_example.yaml",
    }
    assert Registry(storage, host="host-1").pending == reg.pending


def test_import_creates_the_site_with_the_given_or_default_name(tmp_path, reg):
    reg.import_sensor_info(_write_info(tmp_path, f"S: {ADDR1}\n"), "lab-a", site_name="Lab A")
    reg.import_sensor_info(_write_info(tmp_path, f"S: {ADDR2}\n", "b.yaml"), "lab-b")
    assert reg.sites["lab-a"].name == "Lab A"
    assert reg.sites["lab-b"].name == "lab-b"


def test_import_into_an_existing_site_keeps_its_name(tmp_path, reg):
    reg.add_site("lab-a", "Lab A")
    reg.import_sensor_info(_write_info(tmp_path, f"S: {ADDR1}\n"), "lab-a", site_name="Renamed")
    assert reg.sites["lab-a"].name == "Lab A"
    assert len(reg.pending) == 1


@pytest.mark.parametrize(
    ("text", "lineno"),
    [
        (f"S1: {ADDR1}\nno separator here\n", 2),
        (f"S1: {ADDR1}\n\n# c\nname:{ADDR2}\n", 4),  # colon without a space is not a separator
        (f"S1: {ADDR1}\nS2:\n", 2),
        (f"S1: {ADDR1}\n: {ADDR2}\n", 2),
        (f"S1: {ADDR1}\nS2: ''\n", 2),
    ],
    ids=["no-colon", "colon-no-space", "empty-address", "empty-name", "empty-quoted-address"],
)
def test_import_bad_line_is_all_or_nothing(tmp_path, reg, storage, text, lineno):
    path = _write_info(tmp_path, text)
    with pytest.raises(StorageError, match=rf"sensor_info_example\.yaml:{lineno}:"):
        reg.import_sensor_info(path, "lab-a", site_name="Lab A")
    assert reg.pending == () and "lab-a" not in reg.sites
    assert not storage.registry_path.exists()


def test_import_missing_or_undecodable_file_raises_storage_error(tmp_path, reg):
    with pytest.raises(StorageError):
        reg.import_sensor_info(tmp_path / "missing.yaml", "lab-a")
    bad = tmp_path / "latin.yaml"
    bad.write_bytes(b"S: \xe9\n")
    with pytest.raises(StorageError):
        reg.import_sensor_info(bad, "lab-a")


def test_reimport_adds_only_new_entries(tmp_path, reg, storage, monkeypatch):
    path = _write_info(tmp_path, f"S1: {ADDR1}\nS2: {ADDR2}\n")
    assert len(reg.import_sensor_info(path, "lab-a")) == 2

    saves = []
    real = storage.write_json_atomic
    monkeypatch.setattr(storage, "write_json_atomic", lambda p, d: (saves.append(p), real(p, d)))
    assert reg.import_sensor_info(path, "lab-a") == []
    assert saves == []  # nothing new, nothing written
    assert len(reg.pending) == 2

    path.write_text(f"S1: {ADDR1.upper()}\nS2: {ADDR2}\nS3: {MAC}\n", encoding="utf-8")
    added = reg.import_sensor_info(path, "lab-a")
    assert [p.alias for p in added] == ["S3"]
    assert [p.alias for p in reg.pending] == ["S1", "S2", "S3"]


def test_import_skips_duplicates_within_the_file(tmp_path, reg):
    added = reg.import_sensor_info(_write_info(tmp_path, f"S1: {ADDR1}\nSame: {ADDR1}\n"), "lab-a")
    assert [p.alias for p in added] == ["S1"]


def test_import_skips_addresses_already_cached_for_a_sensor_on_this_host(tmp_path, reg):
    reg.add_site("lab-a", "Lab A")
    reg.add_sensor(ID1, "lab-a", "Sensor 1")
    reg.match(ID1, ADDR1)
    added = reg.import_sensor_info(_write_info(tmp_path, f"S1: {ADDR1.upper()}\nS2: {ADDR2}\n"), "lab-a")
    assert [p.alias for p in added] == ["S2"]


def test_import_on_another_host_is_independent(tmp_path, storage):
    path = _write_info(tmp_path, f"S1: {ADDR1}\n")
    Registry(storage, host="host-1").import_sensor_info(path, "lab-a")
    other = Registry(storage, host="host-2")
    added = other.import_sensor_info(path, "lab-a")
    assert [(p.host, p.address) for p in added] == [("host-2", ADDR1)]
    assert [p.host for p in other.pending] == ["host-1", "host-2"]


def test_same_address_in_two_sites_is_kept_per_site_until_resolved(tmp_path, reg):
    reg.import_sensor_info(_write_info(tmp_path, f"S1: {ADDR1}\n"), "lab-a")
    added = reg.import_sensor_info(_write_info(tmp_path, f"S1b: {ADDR1}\n", "b.yaml"), "lab-b")
    assert [p.site_id for p in reg.pending] == ["lab-a", "lab-b"] and len(added) == 1
