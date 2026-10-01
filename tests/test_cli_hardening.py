"""CLI orchestration hardening (ms605.cli.cli): batch isolation, clone validation,
push-handler hygiene, gather keep-alives, data-dir resolution, gather cleanup and
menu error handling. Flows run against the protocol-level simulator (ms605.sim)
through the real driver -- no BLE hardware; every address/name is synthetic."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import ms605.cli.cli as cli
import ms605.driver as driver_mod
from ms605 import MS605, ConfigProfile, MS605DeviceError, MS605TimeoutError
from ms605.cli._shared import LiveLink
from ms605.cli.cli import ManagedDevice
from ms605.models import encode_zone_thresholds
from ms605.protocol import TAG_SENSITIVITY, TAG_ZONE_THRESHOLDS
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet


def _run(coro):
    return asyncio.run(coro)


def _patch_fleet(monkeypatch, fleet: SimFleet, *, delays: dict[str, float] | None = None) -> None:
    """Route the real driver's scanner and client through `fleet`; `delays`
    makes a device's connect() take that many wall seconds."""
    delays = delays or {}

    def factory(address_or_device, *args, **kwargs):
        client = fleet.client_factory(address_or_device, *args, **kwargs)
        delay = delays.get(client.address, 0.0)
        if delay:
            real_connect = client.connect

            async def slow_connect(**kw):
                await asyncio.sleep(delay)
                return await real_connect(**kw)

            client.connect = slow_connect
        return client

    monkeypatch.setattr(driver_mod, "BleakScanner", fleet)
    monkeypatch.setattr(driver_mod, "BleakClient", factory)
    monkeypatch.setattr(MS605, "_scan_rssi", {})


def _fleet(count: int, **kw) -> SimFleet:
    kw.setdefault("connectable_window", None)
    kw.setdefault("idle_timeout", None)
    kw.setdefault("speed", 100)
    return SimFleet(count, **kw)


async def _managed(dev) -> ManagedDevice:
    ms = MS605(dev.ble_device)
    await ms.connect(timeout=1.0)
    return ManagedDevice(ms=ms, device=dev.ble_device, name=dev.name, address=dev.address)


def _no_stray_tasks() -> bool:
    return asyncio.all_tasks() == {asyncio.current_task()}


async def _pick_all(_message, options, checked=None):
    return [value for _label, value in options]


# -- 1. batch calibration: one sensor's failure stays its own -----------------------


def test_batch_unexpected_error_on_one_sensor_does_not_abort_or_release_the_rest(monkeypatch, tmp_path, capsys):
    fleet = _fleet(3, calibration_secs=30)
    _patch_fleet(monkeypatch, fleet)
    history = tmp_path / "history.jsonl"
    monkeypatch.setattr(cli, "CALIBRATION_HISTORY_PATH", history)

    async def gather(_scan, _timeout, _log, *, pool_out, **_kw):
        for dev in fleet.devices:
            pool_out.append(await _managed(dev))
        async def boom(**_kw):
            raise RuntimeError("synthetic failure")
        pool_out[0].ms.start_auto_calibration = boom  # not an MS605Error

    async def no_gate(*_a, **_kw):
        return None

    monkeypatch.setattr(cli, "_batch_gather_menu", gather)
    monkeypatch.setattr(cli, "_batch_await_fire_trigger", no_gate)

    code = _run(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=1.0, calibration_timeout=30.0))
    out = capsys.readouterr().out

    assert code == 1
    assert "최종 결과" in out and "2/3대 보정 성공" in out
    assert "RuntimeError: synthetic failure" in out
    # the healthy sensors were allowed to finish learning before being released
    for dev in fleet.devices[1:]:
        assert dev.thresholds == list(DEFAULT_LEARNED_THRESHOLDS)
        assert not dev.connected
    assert len(history.read_text().splitlines()) == 2


# -- 2. clone: validate once up front, a bad target never stops the loop ------------


def test_clone_rejects_an_invalid_profile_before_connecting_any_target(monkeypatch, capsys):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)
    source = fleet.devices[0]

    async def pick_source(*_a, **_kw):
        return source.ble_device

    async def no_targets(*_a, **_kw):
        raise AssertionError("a target was gathered for an invalid profile")

    bad = ConfigProfile(sensitivity=9)  # outside 1..4
    monkeypatch.setattr(ConfigProfile, "from_config", classmethod(lambda cls, *_a, **_kw: bad))
    monkeypatch.setattr(cli, "discover_and_select", pick_source)
    monkeypatch.setattr(cli, "_batch_gather_menu", no_targets)

    code = _run(cli.run_clone(scan_secs=0.01, connect_timeout=1.0, only="sensitivity", assume_yes=True))

    assert code == 2
    assert "올바르지 않" in capsys.readouterr().out
    assert not source.connected  # the source link was still closed


def test_clone_apply_all_continues_after_an_unexpected_error(monkeypatch):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)

    async def run():
        first, second = [await _managed(d) for d in fleet.devices]

        async def boom(*_a, **_kw):
            raise RuntimeError("synthetic failure")

        first.ms.apply_profile = boom
        await cli._clone_apply_all([first, second], ConfigProfile(sensitivity=3), ["sensitivity"], lambda _m: None)
        return first, second

    first, second = _run(run())
    assert first.status == "clone_error" and "RuntimeError" in first.detail
    assert second.status == "clone_ok"
    assert fleet.devices[1].sensitivity == 3


# -- 3. auto-calibration flow leaves no push handler / heartbeat behind -------------


def test_auto_calibration_flow_removes_its_push_handler_and_heartbeat(monkeypatch, tmp_path, capsys):
    fleet = _fleet(1, calibration_secs=30)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "CALIBRATION_HISTORY_PATH", tmp_path / "history.jsonl")

    async def no_gate(_link):
        return None

    monkeypatch.setattr(cli, "_await_calibration_trigger", no_gate)

    async def run():
        dev = fleet.devices[0]
        md = await _managed(dev)
        link = LiveLink(md.ms, dev.ble_device, 0.01, 1.0)
        for _ in range(2):  # repeated runs must not stack handlers
            await cli.flow_auto_calibration(link)
            assert md.ms._push_handlers == []
            assert _no_stray_tasks()
        capsys.readouterr()
        await md.ms.set_live_output(True)  # pushes keep coming; nobody should print them
        await asyncio.sleep(0.1)
        return capsys.readouterr().out

    assert "Z0:" not in _run(run())


def test_auto_calibration_flow_cleanup_does_not_swallow_a_cancel(monkeypatch, tmp_path, capsys):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "CALIBRATION_HISTORY_PATH", tmp_path / "history.jsonl")

    async def no_gate(_link):
        return None

    monkeypatch.setattr(cli, "_await_calibration_trigger", no_gate)

    async def run():
        dev = fleet.devices[0]
        md = await _managed(dev)
        link = LiveLink(md.ms, dev.ble_device, 0.01, 1.0)
        flow: asyncio.Task | None = None

        async def start_then_get_cancelled(**_kw):
            # the cancel lands while the flow's cleanup awaits its heartbeat task
            asyncio.get_running_loop().call_soon(flow.cancel)
            return False

        md.ms.start_auto_calibration = start_then_get_cancelled
        flow = asyncio.create_task(cli.flow_auto_calibration(link))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(flow, 1.0)
        await md.ms.disconnect()

    _run(run())


def _handler_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]


def test_live_monitor_skips_a_short_tag55_without_a_handler_traceback(monkeypatch, caplog, capsys):
    fleet = _fleet(1, speed=1000)  # one tag55 per ms
    _patch_fleet(monkeypatch, fleet)

    async def enter_later(*_a):
        await asyncio.sleep(0.05)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    async def run():
        dev = fleet.devices[0]
        md = await _managed(dev)
        dev.corrupt_live("short")
        with caplog.at_level("ERROR", logger="ms605"):
            await cli._flow_live_monitor_plain(LiveLink(md.ms, dev.ble_device, 0.01, 1.0))

    _run(run())
    assert _handler_errors(caplog) == []  # no traceback over the display
    assert "Z0:" in capsys.readouterr().out  # the pushes after it still render


def test_auto_calibration_flow_skips_a_short_tag55_without_a_handler_traceback(monkeypatch, tmp_path, caplog):
    fleet = _fleet(1, calibration_secs=30)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "CALIBRATION_HISTORY_PATH", tmp_path / "history.jsonl")

    async def no_gate(_link):
        return None

    monkeypatch.setattr(cli, "_await_calibration_trigger", no_gate)

    async def run():
        dev = fleet.devices[0]
        md = await _managed(dev)
        dev.corrupt_live("short")
        with caplog.at_level("ERROR", logger="ms605"):
            await cli.flow_auto_calibration(LiveLink(md.ms, dev.ble_device, 0.01, 1.0))
        return dev.sensitivity

    assert _run(run()) == 4  # the run itself still completed (CUSTOM)
    assert _handler_errors(caplog) == []


# -- 4. keep-alive while gathering / waiting ---------------------------------------


def test_menu_gather_pings_early_sensors_while_later_ones_connect(monkeypatch):
    fleet = _fleet(2, speed=1)
    fleet.devices[0].idle_timeout = 0.5  # drops after 0.5 s without a central write
    _patch_fleet(monkeypatch, fleet, delays={fleet.devices[1].address: 0.9})
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)

    async def run():
        pool: list[ManagedDevice] = []
        await cli._batch_gather_menu(0.01, 5.0, lambda _m: None, pool_out=pool, keepalive_interval=0.1)
        alive = fleet.devices[0].connected
        await cli._batch_release(pool, lambda _m: None)
        assert _no_stray_tasks()
        return len(pool), alive

    assert _run(run()) == (2, True)


def test_collect_gather_pings_while_waiting_for_enter(monkeypatch):
    fleet = _fleet(1, speed=1)
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)

    async def enter_later(_prompt):
        await asyncio.sleep(0.9)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    async def run():
        pool: list[ManagedDevice] = []
        await cli._batch_gather_collect(0.01, 5.0, lambda _m: None, pool_out=pool, keepalive_interval=0.1)
        alive = fleet.devices[0].connected
        status = pool[0].status
        await cli._batch_release(pool, lambda _m: None)
        return alive, status

    assert _run(run()) == (True, "connected")


def test_failed_keepalive_ping_marks_the_sensor_lost(monkeypatch):
    fleet = _fleet(1, speed=1)
    _patch_fleet(monkeypatch, fleet)

    async def enter_later(_prompt):
        await asyncio.sleep(0.5)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    async def run():
        pool: list[ManagedDevice] = []
        logs: list[str] = []
        fleet.devices[0].drop_link(after=0.2)
        await cli._batch_gather_collect(0.01, 5.0, logs.append, pool_out=pool, keepalive_interval=0.1)
        await cli._batch_release(pool, logs.append)
        return pool[0], logs

    md, logs = _run(run())
    assert md.status == "lost" and md.detail
    assert any("연결 끊김" in line for line in logs)


def test_fire_gate_keeps_pinging_and_stops_pinging_on_enter(monkeypatch):
    fleet = _fleet(1, speed=1)
    fleet.devices[0].idle_timeout = 0.5

    async def enter_later(_prompt):
        await asyncio.sleep(0.9)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)
    _patch_fleet(monkeypatch, fleet)

    async def run():
        md = await _managed(fleet.devices[0])
        await cli._batch_await_fire_trigger([md], 0.1, lambda _m: None)
        alive = fleet.devices[0].connected
        stray = _no_stray_tasks()
        await cli._batch_release([md], lambda _m: None)
        return alive, stray, md.status

    assert _run(run()) == (True, True, "connected")


def test_scheduled_hold_keeps_links_alive_until_the_target_time(monkeypatch):
    fleet = _fleet(1, speed=1)
    fleet.devices[0].idle_timeout = 0.4
    _patch_fleet(monkeypatch, fleet)

    async def run():
        md = await _managed(fleet.devices[0])
        await cli._batch_hold([md], datetime.now() + timedelta(seconds=0.9), 0.1, lambda _m: None)
        alive = fleet.devices[0].connected
        stray = _no_stray_tasks()
        await cli._batch_release([md], lambda _m: None)
        return alive, stray

    assert _run(run()) == (True, True)


def test_clone_confirm_wait_keeps_targets_connected(monkeypatch, tmp_path):
    fleet = _fleet(1, speed=1)
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)

    async def slow_confirm(_message, *, default=False):
        await asyncio.sleep(0.9)  # the operator reads the target list
        return True

    monkeypatch.setattr(cli._ui, "confirm", slow_confirm)
    profile_path = cli.save_profile(ConfigProfile(sensitivity=3), tmp_path / "profile.json")

    code = _run(
        cli.run_clone(
            scan_secs=0.01, connect_timeout=1.0, from_file=str(profile_path), only="sensitivity", keepalive_interval=0.1
        )
    )

    assert code == 0
    assert fleet.devices[0].sensitivity == 3


def test_clone_keeps_queued_targets_alive_while_earlier_ones_are_written(monkeypatch, tmp_path):
    fleet = _fleet(2, speed=1)
    for dev in fleet.devices:
        dev.idle_timeout = 0.5
        dev.response_delay = 0.1  # 7 writes + a read-back: well past one idle window per target
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)
    profile = ConfigProfile(
        sensitivity=4,
        detect_mode=1,
        zone_enable=[True, True, True, True, True, False, False],
        zone_thresholds=[(120 - 10 * z, 60 - 5 * z) for z in range(7)],
        subsensor_zones=[[0, 1], [2, 3], [4, 5, 6]],
        subsensor_timing=[(10, 60), (20, 90), (30, 120)],
        subsensor_enable=[True, False, True],
    )
    profile_path = cli.save_profile(profile, tmp_path / "profile.json")

    code = _run(
        cli.run_clone(
            scan_secs=0.01, connect_timeout=1.0, from_file=str(profile_path), assume_yes=True, keepalive_interval=0.1
        )
    )

    assert code == 0
    for dev in fleet.devices:
        assert dev.thresholds == profile.zone_thresholds


def test_clone_validates_only_the_selected_sections(monkeypatch):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)
    source, target = fleet.devices
    target_pairs = [(100 + z, 50 + z) for z in range(7)]
    # the source reports tag 61 = 0; capture and the target read-back both decode for real
    source.tags[TAG_SENSITIVITY] = b"\x00"
    source.tags[TAG_ZONE_THRESHOLDS] = encode_zone_thresholds(target_pairs)
    assert target.thresholds != target_pairs

    async def pick_source(*_a, **_kw):
        return source.ble_device

    monkeypatch.setattr(cli, "discover_and_select", pick_source)

    code = _run(cli.run_clone(scan_secs=0.01, connect_timeout=1.0, only="zone_thresholds", assume_yes=True))

    assert code == 0
    assert target.thresholds == target_pairs


def test_keepalive_device_error_status_keeps_the_sensor_and_keeps_pinging(monkeypatch):
    fleet = _fleet(1, speed=1)
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)

    async def run():
        md = await _managed(fleet.devices[0])
        real_ping = md.ms.ping
        calls = []

        async def flaky_ping(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise MS605DeviceError(7, 1)  # answered, but with an error status
            await real_ping(**kwargs)

        md.ms.ping = flaky_ping
        logs: list[str] = []
        cli._start_keepalive(md, 0.1, logs.append)
        await asyncio.sleep(0.9)
        alive = fleet.devices[0].connected
        await cli._batch_release([md], logs.append)
        return md.status, alive, len(calls)

    status, alive, pings = _run(run())
    assert status == "connected" and alive
    assert pings >= 3


@pytest.mark.parametrize("fault", ["corrupt_next_crc", "drop_responses"])
def test_keepalive_ping_unanswered_on_a_live_link_keeps_the_sensor(monkeypatch, fault):
    fleet = _fleet(1, speed=1)
    _patch_fleet(monkeypatch, fleet)
    dev = fleet.devices[0]

    async def run():
        md = await _managed(dev)
        real_ping = md.ms.ping
        calls = []

        async def short_ping(**_kw):
            calls.append(1)
            await real_ping(timeout=0.05)

        md.ms.ping = short_ping
        getattr(dev, fault)()  # the first keep-alive ACK is corrupted / never sent
        logs: list[str] = []
        cli._start_keepalive(md, 0.05, logs.append)
        await asyncio.sleep(0.5)
        alive = dev.connected
        await cli._batch_release([md], logs.append)
        return md.status, alive, len(calls), logs

    status, alive, pings, logs = _run(run())
    assert status == "connected" and alive
    assert pings >= 3
    assert any("응답 없음" in line for line in logs)


def test_batch_release_is_bounded_with_a_keepalive_ping_stuck_mid_write(monkeypatch):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)

    async def run():
        md = await _managed(fleet.devices[0])
        client = md.ms._client
        stuck = asyncio.Event()

        async def forever(*_a, **_kw):
            stuck.set()
            await asyncio.Event().wait()

        client.write_gatt_char = forever
        cli._start_keepalive(md, 0.01, lambda _m: None)
        await asyncio.wait_for(stuck.wait(), timeout=1.0)
        await asyncio.wait_for(cli._batch_release([md], lambda _m: None), timeout=1.0)
        return md.ms.is_connected, _no_stray_tasks()

    assert _run(run()) == (False, True)


# -- 5. data dir -----------------------------------------------------------------


def test_data_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path / "custom"))
    assert cli.resolve_data_dir() == tmp_path / "custom"


def test_data_dir_is_the_repo_root_for_a_source_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path)
    assert cli.resolve_data_dir() == tmp_path


def test_data_dir_is_the_user_data_dir_when_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path / "site-packages")  # no pyproject.toml there
    seen: list[str] = []

    def fake_user_data_dir(appname, **_kw):
        seen.append(appname)
        return str(tmp_path / "userdata")

    monkeypatch.setattr(cli, "user_data_dir", fake_user_data_dir)
    assert cli.resolve_data_dir() == tmp_path / "userdata"
    assert seen == ["ms605"]


def test_save_calibration_record_defaults_to_the_history_path(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "history.jsonl"
    monkeypatch.setattr(cli, "CALIBRATION_HISTORY_PATH", target)
    assert cli.save_calibration_record({"a": 1}) == target
    assert json.loads(target.read_text()) == {"a": 1}


# -- 6. --collect cleanup ----------------------------------------------------------


def test_interrupted_collect_cancels_scanner_and_in_flight_connects(monkeypatch):
    fleet = _fleet(1)
    dev = fleet.devices[0]
    _patch_fleet(monkeypatch, fleet, delays={dev.address: 0.2})

    async def hang(_prompt):
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "ainput", hang)

    async def run():
        pool: list[ManagedDevice] = []
        task = asyncio.create_task(
            cli._batch_gather_collect(0.01, 5.0, lambda _m: None, pool_out=pool)
        )
        await asyncio.sleep(0.05)  # the connect is in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stray = _no_stray_tasks()
        await asyncio.sleep(0.4)  # long enough for an orphaned connect to land
        return stray, dev.connected, pool

    stray, connected, pool = _run(run())
    assert stray and not connected and pool == []


def test_interrupted_collect_still_leaves_connected_sensors_releasable(monkeypatch):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)

    async def hang(_prompt):
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "ainput", hang)

    async def run():
        pool: list[ManagedDevice] = []
        task = asyncio.create_task(
            cli._batch_gather_collect(0.01, 5.0, lambda _m: None, pool_out=pool)
        )
        for _ in range(100):
            if len(pool) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await cli._batch_release(pool, lambda _m: None)
        return len(pool), [d.connected for d in fleet.devices], _no_stray_tasks()

    assert _run(run()) == (2, [False, False], True)


# -- 7. main menu survives a failed action -----------------------------------------


def _menu_link(monkeypatch, choices: list[str]):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)
    picks = iter(choices)

    async def select(_message, _options):
        return next(picks)

    monkeypatch.setattr(cli._ui, "select", select)
    return fleet


def test_menu_reports_a_driver_error_and_returns_to_the_menu(monkeypatch, capsys):
    fleet = _menu_link(monkeypatch, ["3", "3", "q"])

    async def run():
        md = await _managed(fleet.devices[0])
        link = LiveLink(md.ms, fleet.devices[0].ble_device, 0.01, 1.0)
        real = md.ms.read_config
        calls = 0

        async def flaky(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise MS605TimeoutError("no response")
            return await real(*a, **kw)

        md.ms.read_config = flaky
        await cli.main_menu(link)
        return calls

    assert _run(run()) == 2  # the second "read config" ran: the session survived
    out = capsys.readouterr().out
    assert "작업 실패" in out and "no response" in out


def test_menu_still_propagates_cancellation(monkeypatch):
    fleet = _menu_link(monkeypatch, ["3", "q"])

    async def run():
        md = await _managed(fleet.devices[0])
        link = LiveLink(md.ms, fleet.devices[0].ble_device, 0.01, 1.0)

        async def cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        md.ms.read_config = cancelled
        await cli.main_menu(link)

    with pytest.raises(asyncio.CancelledError):
        _run(run())

