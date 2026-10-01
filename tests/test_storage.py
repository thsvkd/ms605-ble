"""ms605.storage: data root resolution, atomic JSON files, the calibration
history log and config snapshots. Everything lives under tmp_path (or
MS605_DATA_DIR pointed at it); the real user data dir is never touched. Every
value here is synthetic."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from platformdirs import user_data_dir

import ms605.storage as storage_mod
from ms605 import MS605
from ms605.errors import StorageError
from ms605.models import ConfigProfile
from ms605.sim import SimMS605
from ms605.storage import Storage, data_root

DEVICE_ID = "00" * 19 + "01"
OTHER_ID = "00" * 19 + "02"
ADDR = "00000000-0000-4000-8000-000000000001"


def _record(**extra) -> dict:
    return {
        "timestamp": "2026-10-01T12:00:00+00:00",
        "device_name": "MRBL_SIM01",
        "device_address": ADDR,
        "sensitivity": 4,
        "detect_mode": 2,
        "zones": [{"index": 0, "distance_m": 0.8, "trigger": 70, "maintain": 30}],
        **extra,
    }


def _sim_profile() -> ConfigProfile:
    """The full config of a simulated device, as a profile (what apply snapshots)."""

    async def read() -> ConfigProfile:
        dev = SimMS605(1)
        dev.press_button()
        ms = MS605(dev.address, client_factory=dev.client_factory, inter_chunk_delay=0)
        await ms.connect()
        try:
            return ConfigProfile.from_config(await ms.read_config())
        finally:
            await ms.disconnect()

    return asyncio.run(read())


# -- data root --------------------------------------------------------------


def test_data_root_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path / "data"))
    assert data_root() == tmp_path / "data"
    assert Storage().root == tmp_path / "data" / "cal_results"


def test_data_root_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MS605_DATA_DIR", "~/ms605-data")
    assert data_root() == tmp_path / "ms605-data"


def test_data_root_source_checkout_uses_repo_root(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    monkeypatch.setattr(storage_mod, "_REPO_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text("")
    assert data_root() == tmp_path


def test_data_root_installed_uses_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    monkeypatch.setattr(storage_mod, "_REPO_ROOT", tmp_path)  # no pyproject.toml here
    assert data_root() == Path(user_data_dir("ms605", appauthor=False))


def test_storage_paths(tmp_path):
    s = Storage(tmp_path)
    assert s.root == tmp_path
    assert s.registry_path == tmp_path / "registry.json"
    assert s.history_path == tmp_path / "calibration_history.jsonl"
    assert s.snapshots_dir == tmp_path / "snapshots"


# -- JSON files -------------------------------------------------------------


def test_read_json_missing_is_none(tmp_path):
    assert Storage(tmp_path).read_json(tmp_path / "nope.json") is None


def test_read_json_corrupt_raises_storage_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"a": ')
    with pytest.raises(StorageError, match="bad.json"):
        Storage(tmp_path).read_json(path)


def test_read_json_non_object_raises_storage_error(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]")
    with pytest.raises(StorageError):
        Storage(tmp_path).read_json(path)


def test_read_json_unreadable_raises_storage_error(tmp_path):
    with pytest.raises(StorageError):
        Storage(tmp_path).read_json(tmp_path)  # a directory


def test_read_json_non_utf8_raises_storage_error(tmp_path):
    path = tmp_path / "latin.json"
    path.write_bytes(b'{"a": "\xe9"}')
    with pytest.raises(StorageError):
        Storage(tmp_path).read_json(path)


def test_write_json_atomic_roundtrip_and_format(tmp_path):
    s = Storage(tmp_path)
    path = tmp_path / "deep" / "er" / "x.json"
    s.write_json_atomic(path, {"name": "센서", "n": 1})
    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "name": "센서",\n  "n": 1\n}\n'
    assert s.read_json(path) == {"name": "센서", "n": 1}
    assert [p.name for p in path.parent.iterdir()] == ["x.json"]


def test_write_json_atomic_replace_failure_keeps_old_file(monkeypatch, tmp_path):
    s = Storage(tmp_path)
    path = tmp_path / "x.json"
    s.write_json_atomic(path, {"v": 1})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StorageError, match="disk full"):
        s.write_json_atomic(path, {"v": 2})
    monkeypatch.undo()
    assert s.read_json(path) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # temp file cleaned up


def test_write_json_atomic_write_failure_cleans_temp(monkeypatch, tmp_path):
    s = Storage(tmp_path)

    def boom(fd):
        raise OSError("io error")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(StorageError, match="io error"):
        s.write_json_atomic(tmp_path / "x.json", {"v": 1})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", [{"alias": "\ud800"}, {"v": {1, 2}}], ids=["lone-surrogate", "not-serialisable"])
def test_write_json_atomic_unencodable_data_is_a_storage_error_and_leaves_no_temp(tmp_path, bad):
    s = Storage(tmp_path)
    s.write_json_atomic(tmp_path / "x.json", {"v": 1})
    with pytest.raises(StorageError):
        s.write_json_atomic(tmp_path / "x.json", bad)
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
    assert json.loads((tmp_path / "x.json").read_text()) == {"v": 1}


def test_write_json_atomic_unwritable_parent_raises_storage_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StorageError):
        Storage(tmp_path).write_json_atomic(blocker / "x.json", {})


# -- calibration history ----------------------------------------------------


def test_history_missing_file_is_empty(tmp_path):
    assert Storage(tmp_path).read_history() == []


def test_append_history_writes_one_json_line_per_record(tmp_path):
    s = Storage(tmp_path / "new-dir")
    path = s.append_history(_record(device_name="센서"))
    s.append_history(_record(device_id=DEVICE_ID))
    assert path == s.history_path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["device_name"] == "센서"
    assert "센서" in lines[0]  # not \u-escaped
    assert list(json.loads(lines[1]))[-1] == "device_id"  # new key goes last, old order kept
    assert [r["timestamp"] for r in s.read_history()] == ["2026-10-01T12:00:00+00:00"] * 2


def test_append_history_failure_raises_storage_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StorageError):
        Storage(blocker).append_history(_record())


def test_read_history_skips_corrupt_lines_with_warning(tmp_path, caplog):
    s = Storage(tmp_path)
    s.append_history(_record(sensitivity=1))
    with s.history_path.open("a", encoding="utf-8") as f:
        f.write("not json\n[1]\n\n")
    s.append_history(_record(sensitivity=2))
    with s.history_path.open("a", encoding="utf-8") as f:
        f.write('{"truncated": ')  # last line cut off mid-write, no newline
    with caplog.at_level(logging.WARNING, logger="ms605.storage"):
        records = s.read_history()
    assert [r["sensitivity"] for r in records] == [1, 2]
    assert len(caplog.records) == 3


def test_read_history_filters_by_device_id_and_legacy_address(tmp_path):
    s = Storage(tmp_path)
    s.append_history(_record(sensitivity=1))  # legacy line, this sensor's address
    s.append_history(_record(sensitivity=2, device_id=DEVICE_ID))
    s.append_history(_record(sensitivity=3, device_id=OTHER_ID))  # other sensor, same cached address
    legacy_other = _record(sensitivity=4)
    legacy_other["device_address"] = "00000000-0000-4000-8000-000000000099"
    s.append_history(legacy_other)

    assert [r["sensitivity"] for r in s.read_history()] == [1, 2, 3, 4]
    assert [r["sensitivity"] for r in s.read_history(DEVICE_ID)] == [2]
    assert [r["sensitivity"] for r in s.read_history(DEVICE_ID, addresses=[ADDR.upper()])] == [1, 2]
    assert [r["sensitivity"] for r in s.read_history(OTHER_ID, addresses=[ADDR])] == [1, 3]


def test_read_history_unreadable_raises_storage_error(tmp_path):
    s = Storage(tmp_path)
    s.history_path.mkdir()  # a directory where the file should be
    with pytest.raises(StorageError):
        s.read_history()


# -- snapshots --------------------------------------------------------------


def test_snapshot_roundtrip_from_simulated_device(tmp_path):
    s = Storage(tmp_path)
    profile = _sim_profile()
    snap = s.save_snapshot(DEVICE_ID, profile, ["zone_thresholds", "sensitivity"], "apply")

    assert snap.device_id == DEVICE_ID
    assert snap.reason == "apply"
    assert snap.sections == ("zone_thresholds", "sensitivity")
    assert snap.name.endswith("Z") and len(snap.name) == len("20261001T120000123456Z")
    assert (tmp_path / "snapshots" / DEVICE_ID / f"{snap.name}.json").is_file()
    assert datetime.fromisoformat(snap.taken_at).tzinfo is not None

    loaded = s.load_snapshot(DEVICE_ID, snap.name)
    assert loaded == snap
    assert loaded.profile == profile
    assert len(loaded.profile.sections_present()) == 7  # the full config, not just the written sections
    assert s.list_snapshots(DEVICE_ID) == [loaded]


def test_snapshot_file_format(tmp_path):
    s = Storage(tmp_path)
    snap = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=2), ["sensitivity"], "rollback")
    data = json.loads((s.snapshots_dir / DEVICE_ID / f"{snap.name}.json").read_text())
    assert data["format"] == "ms605-snapshot" and data["version"] == 1
    assert data["device_id"] == DEVICE_ID and data["reason"] == "rollback"
    assert data["sections"] == ["sensitivity"]
    assert data["profile"]["format"] == "ms605-config-profile"


def test_snapshots_listed_newest_first(tmp_path):
    s = Storage(tmp_path)
    saved = [s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=i), ["sensitivity"], "apply") for i in (1, 2, 3)]
    s.save_snapshot(OTHER_ID, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    names = [snap.name for snap in s.list_snapshots(DEVICE_ID)]
    assert names == [snap.name for snap in reversed(saved)]
    assert names == sorted(names, reverse=True)
    assert [snap.profile.sensitivity for snap in s.list_snapshots(DEVICE_ID)] == [3, 2, 1]
    assert s.list_snapshots("00" * 20) == []


def test_snapshot_names_stay_unique_within_one_microsecond(monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(storage_mod, "datetime", FrozenDatetime)
    s = Storage(tmp_path)
    first = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    second = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=2), ["sensitivity"], "apply")
    assert first.name == "20261001T120000123456Z"
    assert second.name == "20261001T120000123457Z"
    assert [x.profile.sensitivity for x in s.list_snapshots(DEVICE_ID)] == [2, 1]


def test_load_snapshot_missing_raises_storage_error(tmp_path):
    with pytest.raises(StorageError, match="no snapshot"):
        Storage(tmp_path).load_snapshot(DEVICE_ID, "20261001T120000123456Z")


def _snapshot_file(s: Storage, mutate) -> str:
    snap = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=2), ["sensitivity"], "apply")
    path = s.snapshots_dir / DEVICE_ID / f"{snap.name}.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    return snap.name


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="something-else"),
        lambda d: d.update(version=2),
        lambda d: d.update(device_id=OTHER_ID),
        lambda d: d.pop("reason"),
        lambda d: d.update(sections="sensitivity"),
        lambda d: d.update(profile={"format": "ms605-config-profile", "sections": {"sensitivity": 99}}),
        lambda d: d.update(profile="nope"),
    ],
    ids=["format", "version", "device_id", "missing-key", "sections-type", "invalid-profile", "profile-type"],
)
def test_load_snapshot_malformed_raises_storage_error(tmp_path, mutate):
    s = Storage(tmp_path)
    name = _snapshot_file(s, mutate)
    with pytest.raises(StorageError):
        s.load_snapshot(DEVICE_ID, name)


def test_load_snapshot_corrupt_json_raises_and_list_skips_it(tmp_path, caplog):
    s = Storage(tmp_path)
    good = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    bad = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=2), ["sensitivity"], "apply")
    (s.snapshots_dir / DEVICE_ID / f"{bad.name}.json").write_text("{")
    with pytest.raises(StorageError):
        s.load_snapshot(DEVICE_ID, bad.name)
    with caplog.at_level(logging.WARNING, logger="ms605.storage"):
        assert s.list_snapshots(DEVICE_ID) == [good]
    assert len(caplog.records) == 1


@pytest.mark.parametrize("bad", ["", "..", ".", "../x", "a/b", "/abs"])
def test_snapshot_paths_cannot_escape_the_snapshot_dir(tmp_path, bad):
    s = Storage(tmp_path)
    with pytest.raises(StorageError):
        s.load_snapshot(DEVICE_ID, bad)
    with pytest.raises(StorageError):
        s.save_snapshot(bad, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    with pytest.raises(StorageError):
        s.list_snapshots(bad)


# -- DND in snapshots (docs/CORE_API.md 2.3) -----------------------------------------


def test_snapshot_keeps_dnd_only_when_given(tmp_path):
    s = Storage(tmp_path)
    plain = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=1), ["sensitivity"], "apply")
    with_dnd = s.save_snapshot(DEVICE_ID, ConfigProfile(sensitivity=1), ["sensitivity", "dnd"], "apply", dnd=False)
    plain_data = json.loads((s.snapshots_dir / DEVICE_ID / f"{plain.name}.json").read_text("utf-8"))
    dnd_data = json.loads((s.snapshots_dir / DEVICE_ID / f"{with_dnd.name}.json").read_text("utf-8"))
    assert "dnd" not in plain_data and plain_data["version"] == 1
    assert dnd_data["dnd"] is False and dnd_data["sections"] == ["sensitivity", "dnd"] and dnd_data["version"] == 1
    assert s.load_snapshot(DEVICE_ID, plain.name).dnd is None  # an older file without the key
    loaded = s.load_snapshot(DEVICE_ID, with_dnd.name)
    assert loaded == with_dnd and loaded.dnd is False


@pytest.mark.parametrize(
    "mutate",
    [lambda d: d.update(sections=["dnd"]), lambda d: d.update(dnd=1), lambda d: d.update(dnd="false")],
    ids=["dnd-section-without-value", "int", "string"],
)
def test_load_snapshot_with_a_bad_dnd_raises_storage_error(tmp_path, mutate):
    s = Storage(tmp_path)
    name = _snapshot_file(s, mutate)
    with pytest.raises(StorageError):
        s.load_snapshot(DEVICE_ID, name)
