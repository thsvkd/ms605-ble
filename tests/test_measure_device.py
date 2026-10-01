"""Offline checks for tools/measure_device.py's pure helpers (hashing /
redaction, result aggregation, ping-interval search, results file) plus the
restore and ping-trial logic against fakes. The hardware measurements
themselves need real sensors and are not exercised here."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "measure_device", Path(__file__).resolve().parent.parent / "tools" / "measure_device.py"
)
md = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_device"] = md  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(md)

SYNTH_MAC = "AA:BB:CC:00:11:22"
SYNTH_UUID = "12345678-1234-1234-1234-1234567890AB"


# -- hashing / redaction ---------------------------------------------------------


def test_short_hash_is_stable_short_and_hides_input():
    h = md.short_hash(SYNTH_MAC)
    assert h == md.short_hash(SYNTH_MAC)
    assert len(h) == md.HASH_LEN
    assert SYNTH_MAC not in h
    assert md.short_hash(SYNTH_MAC) != md.short_hash("AA:BB:CC:00:11:23")
    assert md.short_hash(b"\x01\x02") == md.short_hash(b"\x01\x02")


def test_redact_hash_by_default_raw_with_show_ids():
    assert md.redact(SYNTH_MAC, False) == md.short_hash(SYNTH_MAC)
    assert md.redact(SYNTH_MAC, True) == SYNTH_MAC
    assert md.redact(b"\xde\xad", True) == "dead"
    assert md.redact(b"\xde\xad", False) == md.short_hash(b"\xde\xad")


@pytest.mark.parametrize(
    "text",
    [
        f"device {SYNTH_MAC} not found",
        f"device {SYNTH_MAC.lower()} not found",
        "device AA-BB-CC-00-11-22 not found",
        "/org/bluez/hci0/dev_AA_BB_CC_00_11_22 gone",
        f"No BLE device found matching '{SYNTH_UUID}'.",
        f"peripheral {SYNTH_UUID.lower()} disconnected",
        "connect to RFBL_1A2B3C failed",
    ],
)
def test_scrub_text_removes_address_like_strings(text):
    scrubbed = md.scrub_text(text)
    assert "<addr>" in scrubbed
    for secret in ("BB:CC", "bb:cc", "BB-CC", "BB_CC", "1234-1234", "1A2B3C"):
        assert secret.lower() not in scrubbed.lower()


def test_scrub_text_keeps_ordinary_text():
    assert md.scrub_text("timed out after 10.0s") == "timed out after 10.0s"


def test_scrub_obj_recurses():
    obj = {"a": [f"x {SYNTH_MAC}", {"b": SYNTH_UUID}], "n": 3, "t": (f"{SYNTH_MAC}",)}
    out = md.scrub_obj(obj)
    assert out == {"a": ["x <addr>", {"b": "<addr>"}], "n": 3, "t": ["<addr>"]}


# -- aggregation ------------------------------------------------------------------


def test_summarize_durations_ignores_none_and_handles_empty():
    assert md.summarize_durations([]) == {"count": 0, "min_s": None, "median_s": None, "max_s": None}
    assert md.summarize_durations([None, None])["count"] == 0
    s = md.summarize_durations([10, None, 30, 20])
    assert (s["count"], s["min_s"], s["median_s"], s["max_s"]) == (3, 10.0, 20.0, 30.0)


def test_analyze_attempts_window_and_first_failure():
    attempts = [
        md.Attempt(2.0, False, "e"),
        md.Attempt(4.0, True),
        md.Attempt(6.0, True),
        md.Attempt(8.0, False, "e"),
        md.Attempt(10.0, False, "e"),
    ]
    a = md.analyze_attempts(attempts, first_advert_s=1.5)
    assert a["first_success_s"] == 4.0
    assert a["last_success_s"] == 6.0
    assert a["first_failure_after_success_s"] == 8.0
    assert a["window_s"] == pytest.approx(4.5)
    assert (a["ok_count"], a["fail_count"]) == (2, 3)


def test_analyze_attempts_without_success_or_advert():
    a = md.analyze_attempts([md.Attempt(2.0, False, "e")], first_advert_s=1.0)
    assert a["first_success_s"] is None and a["window_s"] is None
    assert a["first_failure_after_success_s"] is None
    b = md.analyze_attempts([], first_advert_s=None)
    assert b["window_s"] is None and b["ok_count"] == 0


def test_should_stop_window_needs_a_success_then_consecutive_failures():
    f, ok = md.Attempt(0, False), md.Attempt(0, True)
    assert not md.should_stop_window([f, f, f, f], 3)  # never connectable yet: keep trying
    assert not md.should_stop_window([ok, f, f], 3)
    assert md.should_stop_window([ok, f, f, f], 3)
    assert not md.should_stop_window([ok, f, f, ok, f], 3)  # a later success resets the run


# -- ping interval search ------------------------------------------------------------


def _trial(interval, ok):
    return md.IntervalTrial(interval, ok, 1.0)


def test_parse_ladder_sorts_dedupes_and_validates():
    assert md.parse_ladder("30, 10,10,20") == [10, 20, 30]
    for bad in ("", "a,b", "0,5", "-1"):
        with pytest.raises(Exception):  # noqa: B017 - argparse.ArgumentTypeError
            md.parse_ladder(bad)


def test_next_interval_walks_ladder_while_passing():
    ladder = [5, 10, 20]
    assert md.next_interval(ladder, []) == 5
    assert md.next_interval(ladder, [_trial(5, True)]) == 10
    assert md.next_interval(ladder, [_trial(5, True), _trial(10, True)]) == 20
    assert md.next_interval(ladder, [_trial(5, True), _trial(10, True), _trial(20, True)]) is None


def test_next_interval_bisects_after_first_failure_until_resolution():
    ladder = [5, 10, 20, 40]
    trials = [_trial(5, True), _trial(10, True), _trial(20, True), _trial(40, False)]
    assert md.next_interval(ladder, trials, resolution=5) == 30
    trials.append(_trial(30, False))
    assert md.next_interval(ladder, trials, resolution=5) == 25
    trials.append(_trial(25, True))
    assert md.next_interval(ladder, trials, resolution=5) is None  # 25 ok / 30 fail: within resolution


def test_next_interval_stops_when_smallest_fails_or_results_contradict():
    assert md.next_interval([5, 10], [_trial(5, False)]) is None
    flaky = [_trial(5, True), _trial(10, False), _trial(20, True)]
    assert md.next_interval([5, 10, 20, 40], flaky) is None


def test_search_loop_finds_threshold():
    """Drive the search against a link that survives pings <= 27s."""
    trials: list = []
    while (iv := md.next_interval(md.DEFAULT_LADDER, trials, 5)) is not None:
        trials.append(_trial(iv, iv <= 27))
        assert len(trials) < 20
    summary = md.summarize_intervals(trials)
    assert [t.interval for t in trials] == [5, 10, 15, 20, 30, 25]  # ladder up to the failure, then bisect
    assert summary["max_safe_interval_s"] == 25
    assert summary["min_failing_interval_s"] == 30
    assert summary["consistent"] is True


def test_summarize_intervals_flags_inconsistent_results():
    s = md.summarize_intervals([_trial(5, True), _trial(10, False), _trial(20, True)])
    assert s["max_safe_interval_s"] == 20 and s["min_failing_interval_s"] == 10
    assert s["consistent"] is False
    assert md.summarize_intervals([_trial(5, True)])["consistent"] is True
    assert md.summarize_intervals([])["max_safe_interval_s"] is None


# -- zone write helpers ------------------------------------------------------------------


def test_describe_flip():
    assert md.describe_flip(2, 2) == "unchanged"
    assert md.describe_flip(2, 4) == "flipped_to_custom"
    assert md.describe_flip(2, 3) == "changed_other"


def test_perturb_thresholds_differs_and_stays_in_range():
    pairs = [(100, 50)] * 7
    out = md.perturb_thresholds(pairs)
    assert out != pairs and len(out) == 7
    pinned = [(0xFFFF, 0xFFFF)] * 7
    out = md.perturb_thresholds(pinned)
    assert out != pinned and all(0 <= v <= 0xFFFF for p in out for v in p)
    zeros = md.perturb_thresholds([(0, 0)] * 7, delta=-1)
    assert all(0 <= v <= 0xFFFF for p in zeros for v in p)


class _FakeDevice:
    """Models a sensor where tag51 writes flip sensitivity to CUSTOM and a
    preset sensitivity write reloads that preset's thresholds."""

    PRESETS = {1: [(10, 5)] * 7, 2: [(20, 10)] * 7, 3: [(30, 15)] * 7}

    def __init__(self, sensitivity, thresholds):
        self.sensitivity, self.thresholds = sensitivity, list(thresholds)
        self.writes = []

    async def set_sensitivity(self, level):
        self.writes.append(("sens", int(level)))
        self.sensitivity = int(level)
        if self.sensitivity in self.PRESETS:
            self.thresholds = list(self.PRESETS[self.sensitivity])

    async def set_zone_thresholds(self, pairs):
        self.writes.append(("thr", list(pairs)))
        self.thresholds = list(pairs)
        self.sensitivity = 4

    async def read_config(self):
        zones = tuple(SimpleNamespace(trigger=t, maintain=m) for t, m in self.thresholds)
        return SimpleNamespace(sensitivity=self.sensitivity, zone_thresholds=zones)


def test_restore_config_returns_custom_original_exactly():
    original = md.ConfigSnapshot(4, [(11, 6)] * 7)
    dev = _FakeDevice(2, _FakeDevice.PRESETS[2])
    result = asyncio.run(md.restore_config(dev, original))
    assert result["sensitivity_restored"] and result["thresholds_restored"]
    assert (dev.sensitivity, dev.thresholds) == (4, [(11, 6)] * 7)


def test_restore_config_preset_original_with_matching_thresholds():
    original = md.ConfigSnapshot(2, _FakeDevice.PRESETS[2])
    dev = _FakeDevice(4, [(99, 98)] * 7)
    result = asyncio.run(md.restore_config(dev, original))
    assert result["sensitivity_restored"] and result["thresholds_restored"]
    assert dev.sensitivity == 2


def test_restore_config_reports_unrestorable_original():
    # preset sensitivity but hand-tuned thresholds: writing tag51 flips to CUSTOM
    # and re-writing the preset reloads the preset values -> cannot be exact.
    original = md.ConfigSnapshot(2, [(21, 11)] * 7)
    dev = _FakeDevice(2, _FakeDevice.PRESETS[2])
    result = asyncio.run(md.restore_config(dev, original))
    assert not (result["sensitivity_restored"] and result["thresholds_restored"])
    assert result["final"]["sensitivity"] in (2, 4)


# -- ping trial -----------------------------------------------------------------------------


class _FakePingLink:
    def __init__(self, *, drop_after_pings=None, fail_ping=False):
        self.is_connected = True
        self.pings = 0
        self.drop_after_pings = drop_after_pings
        self.fail_ping = fail_ping

    async def ping(self):
        self.pings += 1
        if self.fail_ping:
            raise RuntimeError(f"gone {SYNTH_MAC}")
        if self.drop_after_pings is not None and self.pings >= self.drop_after_pings:
            self.is_connected = False


def _ctx(show_ids=False):
    return md.Ctx("unused.json", show_ids, 1.0, 1.0, None)


def test_run_ping_trial_passes_when_link_survives():
    link = _FakePingLink()
    trial = asyncio.run(md.run_ping_trial(_ctx(), link, interval=1, hold=2.6))
    assert trial.ok and trial.interval == 1 and link.pings >= 2


def test_run_ping_trial_detects_drop():
    link = _FakePingLink(drop_after_pings=1)
    trial = asyncio.run(md.run_ping_trial(_ctx(), link, interval=1, hold=10))
    assert not trial.ok and trial.reason == "link_dropped"


def test_run_ping_trial_failed_ping_reason_is_scrubbed():
    trial = asyncio.run(md.run_ping_trial(_ctx(), _FakePingLink(fail_ping=True), interval=1, hold=10))
    assert not trial.ok and trial.reason.startswith("ping_failed")
    assert SYNTH_MAC not in trial.reason and "<addr>" in trial.reason


# -- device ids -----------------------------------------------------------------------------


def _obs(addr, ident):
    return {"address_hash": addr, "id_hash": ident}


def test_analyze_device_ids_unique_and_stable():
    a = md.analyze_device_ids([_obs("a1", "i1"), _obs("a2", "i2"), _obs("a1", "i1")])
    assert a["unique"] and a["stable"]
    assert (a["distinct_devices"], a["distinct_ids"], a["observations"]) == (2, 2, 3)


def test_analyze_device_ids_detects_shared_and_unstable():
    shared = md.analyze_device_ids([_obs("a1", "i1"), _obs("a2", "i1")])
    assert not shared["unique"] and shared["shared_id_hashes"] == ["i1"]
    unstable = md.analyze_device_ids([_obs("a1", "i1"), _obs("a1", "i2")])
    assert not unstable["stable"] and unstable["unstable_address_hashes"] == ["a1"]
    assert md.analyze_device_ids([])["unique"] is True


def test_collect_id_observations_reads_only_device_id_runs():
    doc = {
        "runs": [
            {
                "measurement": "device-id",
                "result": {"devices": [_obs("a1", "i1"), {"address_hash": "a2", "error": "x"}]},
            },
            {"measurement": "idle-drop", "result": {"devices": [_obs("zz", "zz")]}},
        ]
    }
    assert md.collect_id_observations(doc) == [_obs("a1", "i1")]


# -- results file ------------------------------------------------------------------------------


def test_load_results_missing_file_gives_empty_doc(tmp_path):
    assert md.load_results(str(tmp_path / "none.json")) == {"schema": md.RESULTS_SCHEMA, "runs": []}


def test_load_results_rejects_corrupt_file_without_overwriting(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{not json")
    with pytest.raises(SystemExit):
        md.load_results(str(path))
    assert path.read_text() == "{not json"


def test_default_out_is_in_the_git_ignored_results_dir():
    repo = Path(__file__).resolve().parent.parent
    out = Path(md.DEFAULT_OUT)
    assert out.parent == repo / "cal_results"
    assert "cal_results/" in (repo / ".gitignore").read_text().splitlines()


def test_save_results_creates_the_results_dir(tmp_path):
    path = tmp_path / "cal_results" / "m.json"
    md.save_results(str(path), {"schema": md.RESULTS_SCHEMA, "runs": []})
    assert json.loads(path.read_text())["runs"] == []


def test_run_rejects_a_bad_results_file_before_measuring(tmp_path, monkeypatch):
    path = tmp_path / "foreign.json"
    path.write_text('{"unrelated": true}')
    measured = []

    async def fake_measurement(_ctx, _args):
        measured.append(True)
        return {}

    monkeypatch.setitem(md.MEASUREMENTS, "device-id", fake_measurement)
    parser = md.build_parser()
    args = parser.parse_args(["device-id", "--out", str(path)])
    with pytest.raises(SystemExit):
        asyncio.run(md.run(args, parser))
    assert measured == []  # failed up front, no measurement time wasted
    assert path.read_text() == '{"unrelated": true}'


def test_add_run_scrubs_unless_show_ids():
    result = {"error": f"failed for {SYNTH_MAC}"}
    scrubbed = md.add_run({"runs": []}, "idle-drop", "t", False, result)
    assert scrubbed["runs"][0]["result"] == {"error": "failed for <addr>"}
    raw = md.add_run({"runs": []}, "idle-drop", "t", True, result)
    assert raw["runs"][0]["result"] == result


def test_save_and_reload_accumulates_runs(tmp_path):
    path = str(tmp_path / "out.json")
    for i in range(2):
        doc = md.add_run(md.load_results(path), "device-id", f"t{i}", False, {"devices": []})
        md.save_results(path, doc)
    reloaded = json.loads(Path(path).read_text())
    assert [r["started"] for r in reloaded["runs"]] == ["t0", "t1"]
    assert not Path(path + ".tmp").exists()


# -- CLI ------------------------------------------------------------------------------------------


def test_parser_defaults_for_every_subcommand():
    parser = md.build_parser()
    for name in (*md.MEASUREMENTS, "all"):
        args = parser.parse_args([name])
        assert args.command == name
        for dest in md.COMMON_DESTS:
            assert hasattr(args, dest)
    assert parser.parse_args(["ping-interval", "--ladder", "20,10"]).ladder == [10, 20]
    assert parser.parse_args(["all", "--calibration"]).calibration is True
    assert parser.parse_args(["device-id"]).show_ids is False


def test_ctx_exc_scrubs_addresses_by_default():
    msg = f"boom {SYNTH_MAC}"
    assert SYNTH_MAC not in _ctx().exc(RuntimeError(msg))
    assert SYNTH_MAC in _ctx(show_ids=True).exc(RuntimeError(msg))


def test_ctx_label_hides_address_by_default():
    dev = SimpleNamespace(address=SYNTH_MAC, name="RFBL_1A2B3C")
    label = _ctx().label(dev)
    assert SYNTH_MAC not in label and "RFBL" not in label
    assert SYNTH_MAC in _ctx(show_ids=True).label(dev)
