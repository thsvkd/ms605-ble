"""CLI orchestration hardening (ms605.cli.cli): batch isolation, clone validation,
event-handler hygiene, gather keep-alives, data-dir resolution, gather cleanup and
menu error handling. Flows run against the protocol-level simulator (ms605.sim)
through the real driver and the ms605 core (DeviceSession / Fleet) -- no BLE
hardware; every address/name is synthetic."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

import ms605.cli.cli as cli
import ms605.driver as driver_mod
import ms605.storage as storage_mod
from ms605 import MS605, ConfigProfile, MS605DeviceError, MS605TimeoutError
from ms605.events import EventBus, LinkState
from ms605.models import encode_zone_thresholds
from ms605.protocol import TAG_SENSITIVITY, TAG_ZONE_THRESHOLDS
from ms605.session import DeviceSession
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet
from ms605.storage import Storage


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    """Calibration history and apply snapshots go to a temp data dir, never the repo."""
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path))


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


pytestmark = pytest.mark.usefixtures("no_chunk_pacing")


def _fleet(count: int, **kw) -> SimFleet:
    kw.setdefault("connectable_window", None)
    kw.setdefault("idle_timeout", None)
    kw.setdefault("speed", 100)
    return SimFleet(count, **kw)


def _core(keepalive_interval: float = 15.0, connect_timeout: float = 1.0):
    """The CLI's own Fleet (registry + storage under MS605_DATA_DIR)."""
    return cli._new_fleet(0.01, connect_timeout, keepalive_interval)


async def _session(dev) -> DeviceSession:
    session = DeviceSession(dev.ble_device, EventBus(), connect_timeout=1.0)
    await session.connect()
    return session


def _history() -> list[dict]:
    path = Storage().history_path
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _no_stray_tasks() -> bool:
    return asyncio.all_tasks() == {asyncio.current_task()}


async def _pick_all(_message, options, checked=None):
    return [value for _label, value in options]


async def _enter(*_a):
    return ""


# -- 1. batch calibration: one sensor's failure stays its own -----------------------


def test_batch_unexpected_error_on_one_sensor_does_not_abort_or_release_the_rest(monkeypatch, capsys):
    fleet = _fleet(3, calibration_secs=30)
    _patch_fleet(monkeypatch, fleet)

    async def gather(core, _scan, _log, **_kw):
        sessions = [await core.connect(dev.ble_device) for dev in fleet.devices]

        async def boom(**_kw):
            raise RuntimeError("synthetic failure")

        sessions[0].ms.start_auto_calibration = boom  # not an MS605Error

    monkeypatch.setattr(cli, "_batch_gather_menu", gather)
    monkeypatch.setattr(cli, "ainput", _enter)  # the fire gate

    code = _run(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=1.0, calibration_timeout=30.0))
    out = capsys.readouterr().out

    assert code == 1
    assert "최종 결과" in out and "2/3대 보정 성공" in out
    assert "RuntimeError: synthetic failure" in out
    # the healthy sensors were allowed to finish learning before being released
    for dev in fleet.devices[1:]:
        assert dev.thresholds == list(DEFAULT_LEARNED_THRESHOLDS)
        assert not dev.connected
    assert len(_history()) == 2


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
        core = _core()
        first, second = [await core.connect(d.ble_device) for d in fleet.devices]

        async def boom(*_a, **_kw):
            raise RuntimeError("synthetic failure")

        first.ms.apply_profile = boom
        results = await cli._clone_apply_all(core, ConfigProfile(sensitivity=3), ["sensitivity"], lambda _m: None)
        await core.aclose()
        return cli._clone_status(results[first.device_id]), cli._clone_status(results[second.device_id])

    (first_status, first_detail), (second_status, _) = _run(run())
    assert first_status == "clone_error" and "RuntimeError" in first_detail
    assert second_status == "clone_ok"
    assert fleet.devices[1].sensitivity == 3


# -- 3. auto-calibration flow leaves no event handler / task behind ------------------


async def _no_gate(_session):
    return None


def test_auto_calibration_flow_removes_its_push_handler_and_heartbeat(monkeypatch, capsys):
    fleet = _fleet(1, calibration_secs=10)  # 0.1 s per run
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "_await_calibration_trigger", _no_gate)

    async def run():
        session = await _session(fleet.devices[0])
        handlers = list(session.bus._subscribers)
        tasks = asyncio.all_tasks()  # this test and the session's keep-alive
        for _ in range(2):  # repeated runs must not stack handlers
            await cli.flow_auto_calibration(session)
            assert session.bus._subscribers == handlers
            assert asyncio.all_tasks() == tasks
        capsys.readouterr()
        await session.acquire_live()  # pushes keep coming; nobody should print them
        await asyncio.sleep(0.1)
        out = capsys.readouterr().out
        await session.close()
        return out

    assert "Z0:" not in _run(run())


def test_auto_calibration_flow_cleanup_does_not_swallow_a_cancel(monkeypatch):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "_await_calibration_trigger", _no_gate)

    async def run():
        session = await _session(fleet.devices[0])
        flow: asyncio.Task | None = None

        async def start_then_get_cancelled(**_kw):
            # the cancel lands while the flow's cleanup is still running
            asyncio.get_running_loop().call_soon(flow.cancel)
            return False

        session.ms.start_auto_calibration = start_then_get_cancelled
        flow = asyncio.create_task(cli.flow_auto_calibration(session))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(flow, 1.0)
        await session.close()

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
        session = await _session(dev)
        dev.corrupt_live("short")
        with caplog.at_level("ERROR", logger="ms605"):
            await cli._flow_live_monitor_plain(session)
        await session.close()

    _run(run())
    assert _handler_errors(caplog) == []  # no traceback over the display
    assert "Z0:" in capsys.readouterr().out  # the pushes after it still render


def test_auto_calibration_flow_skips_a_short_tag55_without_a_handler_traceback(monkeypatch, caplog):
    fleet = _fleet(1, calibration_secs=30)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "_await_calibration_trigger", _no_gate)

    async def run():
        dev = fleet.devices[0]
        session = await _session(dev)
        dev.corrupt_live("short")
        with caplog.at_level("ERROR", logger="ms605"):
            await cli.flow_auto_calibration(session)
        await session.close()
        return dev.sensitivity

    assert _run(run()) == 4  # the run itself still completed (CUSTOM)
    assert _handler_errors(caplog) == []


# -- 4. keep-alive while gathering / waiting ---------------------------------------


def test_menu_gather_pings_early_sensors_while_later_ones_connect(monkeypatch):
    fleet = _fleet(2, speed=2)
    fleet.devices[0].idle_timeout = 0.5  # drops after 0.25 s (wall) without a central write
    _patch_fleet(monkeypatch, fleet, delays={fleet.devices[1].address: 0.45})
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)

    async def run():
        core = _core(keepalive_interval=0.05, connect_timeout=5.0)
        await cli._batch_gather_menu(core, 0.01, lambda _m: None)
        alive = fleet.devices[0].connected
        released = await cli._batch_release(core, lambda _m: None)
        assert _no_stray_tasks()
        return len(released), alive

    assert _run(run()) == (2, True)


def test_collect_gather_pings_while_waiting_for_enter(monkeypatch):
    fleet = _fleet(1, speed=2)  # the 0.5 s idle drop is 0.25 s of wall time
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)

    async def enter_later(_prompt):
        await asyncio.sleep(0.45)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    async def run():
        core = _core(keepalive_interval=0.05, connect_timeout=5.0)
        await cli._batch_gather_collect(core, lambda _m: None)
        alive = fleet.devices[0].connected
        (session,) = core.sessions.values()
        state = session.state
        await cli._batch_release(core, lambda _m: None)
        return alive, state

    assert _run(run()) == (True, LinkState.CONNECTED)


def test_failed_keepalive_ping_marks_the_sensor_lost(monkeypatch, capsys):
    fleet = _fleet(1, speed=1)
    _patch_fleet(monkeypatch, fleet)
    answers = iter([0.5, 0.0])  # the collect Enter, then the fire gate

    async def enter_later(_prompt):
        delay = next(answers)
        if delay:
            fleet.devices[0].drop_link(after=0.2)  # while collecting, before the fire
        await asyncio.sleep(delay)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    code = _run(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=5.0, collect=True, keepalive_interval=0.1))
    out = capsys.readouterr().out

    assert code == 1
    assert "연결 끊김 (발사 전 대기 중)" in out
    # the summary carries the lost status with its reason
    (line,) = [ln for ln in out.splitlines() if ln.startswith("  - ") and "->" in ln]
    assert "연결 끊김 (대기 중 유실 -- 버튼 재입력 필요) (" in line


def test_interrupt_at_the_fire_gate_reports_a_sensor_dropped_meanwhile_as_lost(monkeypatch, capsys):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)
    at_gate = asyncio.Event()

    async def hang(_prompt):
        at_gate.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "ainput", hang)

    async def run():
        task = asyncio.create_task(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=1.0))
        await asyncio.wait_for(at_gate.wait(), 2.0)
        fleet.devices[0].drop_link()
        await asyncio.sleep(0.05)
        task.cancel()  # Ctrl-C while the operator is still at the fire gate
        with pytest.raises(asyncio.CancelledError):
            await task
        return _no_stray_tasks()

    assert _run(run())
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith("  - ") and "->" in ln]
    assert len(rows) == 2
    assert "연결 끊김 (대기 중 유실 -- 버튼 재입력 필요) (" in rows[0]  # with its reason
    assert "미보정 (연결 유지된 채 종료)" in rows[1]


def test_collect_enter_lets_a_connect_in_flight_join_the_batch(monkeypatch, capsys):
    fleet = _fleet(2, calibration_secs=30)
    late = fleet.devices[1]
    _patch_fleet(monkeypatch, fleet, delays={late.address: 0.3})
    prompts: list[str] = []

    async def operator(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:  # Enter while the second sensor is still connecting
            while not fleet.devices[0].connected:
                await asyncio.sleep(0.005)
            assert not late.connected
        return ""

    monkeypatch.setattr(cli, "ainput", operator)

    async def run():
        code = await cli.run_batch_calibration(
            scan_secs=0.01, connect_timeout=1.0, collect=True, keepalive_interval=0.15, calibration_timeout=2.0
        )
        return code, _no_stray_tasks()

    assert _run(run()) == (0, True)
    out = capsys.readouterr().out
    assert "건 마무리 대기...)" in out  # the late sensor (and maybe the first, still identifying)
    assert "연결 완료 -- 총 2대" in out and "2/2대 보정 성공" in out


def test_a_corrupt_registry_file_exits_2_with_a_message(monkeypatch, tmp_path, capsys):
    (tmp_path / "cal_results").mkdir()
    (tmp_path / "cal_results" / "registry.json").write_text("{truncated", encoding="utf-8")
    code = _run(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=1.0))
    assert code == 2
    assert "레지스트리 파일 오류" in capsys.readouterr().out


def test_fire_gate_keeps_pinging_and_stops_pinging_on_enter(monkeypatch):
    fleet = _fleet(1, speed=2, calibration_secs=0.3, live_interval=0.1)  # wall time: half of each
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)

    async def enter_later(_prompt):
        await asyncio.sleep(0.45)
        return ""

    monkeypatch.setattr(cli, "ainput", enter_later)

    async def run():
        code = await cli.run_batch_calibration(
            scan_secs=0.01, connect_timeout=5.0, keepalive_interval=0.05, calibration_timeout=5.0
        )
        return code, _no_stray_tasks()

    # the link outlived the 0.45 s wait for Enter, so the fire calibrated it
    assert _run(run()) == (0, True)
    assert fleet.devices[0].thresholds == list(DEFAULT_LEARNED_THRESHOLDS)


def test_scheduled_hold_keeps_links_alive_until_the_target_time(monkeypatch, capsys):
    fleet = _fleet(1, speed=2, calibration_secs=0.3, live_interval=0.1)  # wall time: half of each
    fleet.devices[0].idle_timeout = 0.4
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)
    target = datetime.now() + timedelta(seconds=0.5)
    monkeypatch.setattr(cli, "resolve_target_datetime", lambda _at, _now: target)

    async def run():
        code = await cli.run_batch_calibration(
            scan_secs=0.01, connect_timeout=5.0, schedule="00:00", keepalive_interval=0.05, calibration_timeout=5.0
        )
        return code, _no_stray_tasks()

    assert _run(run()) == (0, True)
    assert datetime.now() >= target  # held until the target, then fired
    assert "대기 단계" in capsys.readouterr().out
    assert fleet.devices[0].thresholds == list(DEFAULT_LEARNED_THRESHOLDS)


def test_clone_confirm_wait_keeps_targets_connected(monkeypatch, tmp_path):
    fleet = _fleet(1, speed=2)  # the 0.5 s idle drop is 0.25 s of wall time
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)

    async def slow_confirm(_message, *, default=False):
        await asyncio.sleep(0.45)  # the operator reads the target list
        return True

    monkeypatch.setattr(cli._ui, "confirm", slow_confirm)
    profile_path = cli.save_profile(ConfigProfile(sensitivity=3), tmp_path / "profile.json")

    code = _run(
        cli.run_clone(
            scan_secs=0.01,
            connect_timeout=1.0,
            from_file=str(profile_path),
            only="sensitivity",
            keepalive_interval=0.05,
        )
    )

    assert code == 0
    assert fleet.devices[0].sensitivity == 3


def test_clone_keeps_queued_targets_alive_while_earlier_ones_are_written(monkeypatch, tmp_path):
    fleet = _fleet(2, speed=4)  # the measured 1 s tag51 read-back lag (0.25 s here): verify must poll
    for dev in fleet.devices:
        dev.idle_timeout = 0.5  # device seconds, like the two below
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
            scan_secs=0.01, connect_timeout=1.0, from_file=str(profile_path), assume_yes=True, keepalive_interval=0.05
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
    fleet = _fleet(1, speed=2)  # the 0.5 s idle drop is 0.25 s of wall time
    fleet.devices[0].idle_timeout = 0.5
    _patch_fleet(monkeypatch, fleet)

    async def run():
        core = _core(keepalive_interval=0.05)
        session = await core.connect(fleet.devices[0].ble_device)
        real_ping = session.ms.ping
        calls = []

        async def flaky_ping(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise MS605DeviceError(7, 1)  # answered, but with an error status
            await real_ping(**kwargs)

        session.ms.ping = flaky_ping
        logs: list[str] = []
        core.bus.subscribe(cli._batch_event_log(core, logs.append))
        await asyncio.sleep(0.45)
        state, alive = session.state, fleet.devices[0].connected
        await cli._batch_release(core, logs.append)
        return state, alive, len(calls)

    state, alive, pings = _run(run())
    assert state is LinkState.CONNECTED and alive
    assert pings >= 3


@pytest.mark.parametrize("fault", ["corrupt_next_crc", "drop_responses"])
def test_keepalive_ping_unanswered_on_a_live_link_keeps_the_sensor(monkeypatch, fault):
    fleet = _fleet(1, speed=1)
    _patch_fleet(monkeypatch, fleet)
    dev = fleet.devices[0]

    async def run():
        core = _core(keepalive_interval=0.05)  # each keep-alive ping waits at most 0.05 s
        session = await core.connect(dev.ble_device)
        real_ping = session.ms.ping
        calls = []

        async def counted_ping(**kw):
            calls.append(1)
            await real_ping(**kw)

        session.ms.ping = counted_ping
        logs: list[str] = []
        core.bus.subscribe(cli._batch_event_log(core, logs.append))
        getattr(dev, fault)()  # the first keep-alive ACK is corrupted / never sent
        await asyncio.sleep(0.5)
        state, alive = session.state, dev.connected
        await cli._batch_release(core, logs.append)
        return state, alive, len(calls), logs

    state, alive, pings, logs = _run(run())
    assert state is LinkState.CONNECTED and alive
    assert pings >= 3
    assert any("응답 없음" in line for line in logs)


def test_batch_release_is_bounded_with_a_keepalive_ping_stuck_mid_write(monkeypatch):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)

    async def run():
        # the production interval: a stuck ping would only give up after
        # min(WRITE_TIMEOUT_S, 15 s) = 10 s, so release must not wait for it
        core = _core(keepalive_interval=15.0)
        session = await core.connect(fleet.devices[0].ble_device)
        stuck = asyncio.Event()

        async def forever(*_a, **_kw):
            stuck.set()
            await asyncio.Event().wait()

        session.ms._client.write_gatt_char = forever
        async with session.operation("read", suspend_keepalive=True):
            pass  # releasing it pings at once
        await asyncio.wait_for(stuck.wait(), timeout=1.0)
        await asyncio.wait_for(cli._batch_release(core, lambda _m: None), timeout=1.0)
        return session.ms.is_connected, _no_stray_tasks()

    assert _run(run()) == (False, True)


def test_keepalive_misses_log_the_pre_m1_message_for_each_kind(monkeypatch):
    fleet = _fleet(1, speed=1)
    _patch_fleet(monkeypatch, fleet)
    dev = fleet.devices[0]

    async def run():
        core = _core(keepalive_interval=0.05)
        session = await core.connect(dev.ble_device)
        real_ping = session.ms.ping
        refusals = [MS605DeviceError(5, 1)]

        async def refused_once(**kw):
            if refusals:
                raise refusals.pop()  # answered, but with an error status
            await real_ping(**kw)

        session.ms.ping = refused_once
        logs: list[str] = []
        core.bus.subscribe(cli._batch_event_log(core, logs.append))
        await _until(lambda: any("keep-alive" in line for line in logs))
        dev.drop_responses(1)  # the one after that is never answered
        await _until(lambda: sum("keep-alive" in line for line in logs) >= 2)
        await cli._batch_release(core, lambda _m: None)
        return [line for line in logs if "keep-alive" in line]

    first, second = _run(run())[:2]
    assert f"{dev.name} {dev.address} keep-alive 응답 오류 (연결 유지): " in first
    assert f"{dev.name} {dev.address} keep-alive 응답 없음 (연결 유지, 재시도): " in second


def test_clone_announces_every_target_including_one_that_is_not_connected(monkeypatch):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)
    gone, kept = fleet.devices

    async def run():
        core = _core()
        first = await core.connect(gone.ble_device)
        await core.connect(kept.ble_device)
        gone.drop_link()
        await _until(lambda: first.state is LinkState.LOST)
        logs: list[str] = []
        await cli._clone_apply_all(core, ConfigProfile(sensitivity=3), ["sensitivity"], logs.append)
        await core.aclose()
        return logs

    logs = _run(run())
    starts = [line for line in logs if "설정 적용 중..." in line]
    assert len(starts) == 2
    assert f"🔧 {gone.name} {gone.address} 설정 적용 중..." in starts[0]
    assert f"🔧 {kept.name} {kept.address} 설정 적용 중..." in starts[1]
    # each announcement comes right before that target's own result line
    assert f"{gone.name} {gone.address} 적용 오류" in logs[logs.index(starts[0]) + 1]


def test_interrupt_mid_fire_keeps_the_results_already_in(monkeypatch, capsys):
    fleet = _fleet(2)
    quick, slow = fleet.devices
    quick.calibration_secs = 10  # 0.1 s of wall time
    slow.calibration_secs = 10_000  # still learning when the operator hits Ctrl-C
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli._ui, "checkbox", _pick_all)
    monkeypatch.setattr(cli, "ainput", _enter)  # fire at once

    async def run():
        task = asyncio.create_task(cli.run_batch_calibration(scan_secs=0.01, connect_timeout=1.0))
        # the history line is written in the same step that reports the quick sensor's result
        await _until(lambda: len(_history()) == 1)
        assert slow.calibrating
        task.cancel()  # Ctrl-C mid-fire
        with pytest.raises(asyncio.CancelledError):
            await task
        return _no_stray_tasks()

    assert _run(run())
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith("  - ") and "->" in ln]
    assert len(rows) == 2
    assert quick.address in rows[0] and "-> 보정 성공" in rows[0]
    assert slow.address in rows[1] and "미보정 (연결 유지된 채 종료)" in rows[1]
    assert "1/2대 보정 성공" in out


def test_auto_calibration_flow_renders_the_jobs_read_back_without_reading_again(monkeypatch, capsys):
    fleet = _fleet(1, calibration_secs=10)
    _patch_fleet(monkeypatch, fleet)
    monkeypatch.setattr(cli, "_await_calibration_trigger", _no_gate)

    async def no_second_read(_session):
        raise cli.MS605Error("synthetic: the flow read the config a second time")

    async def run():
        session = await _session(fleet.devices[0])
        monkeypatch.setattr(cli, "_read_config", no_second_read)
        await cli.flow_auto_calibration(session)
        await session.close()

    _run(run())
    out = capsys.readouterr().out
    assert "반영값 재조회 실패" not in out
    assert "반영된 존별 임계값 (tag51 재조회):" in out
    assert "자동 보정 완료 — 성공적으로 학습되었습니다." in out


async def _until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


# -- 5. data dir -----------------------------------------------------------------


def test_data_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path / "custom"))
    assert storage_mod.data_root() == tmp_path / "custom"


def test_data_dir_is_the_repo_root_for_a_source_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(storage_mod, "_REPO_ROOT", tmp_path)
    assert storage_mod.data_root() == tmp_path


def test_data_dir_is_the_user_data_dir_when_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("MS605_DATA_DIR", raising=False)
    monkeypatch.setattr(storage_mod, "_REPO_ROOT", tmp_path / "site-packages")  # no pyproject.toml there
    seen: list[str] = []

    def fake_user_data_dir(appname, **_kw):
        seen.append(appname)
        return str(tmp_path / "userdata")

    monkeypatch.setattr(storage_mod, "user_data_dir", fake_user_data_dir)
    assert storage_mod.data_root() == tmp_path / "userdata"
    assert seen == ["ms605"]


def test_append_history_defaults_to_the_history_path_under_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MS605_DATA_DIR", str(tmp_path / "nested"))
    target = tmp_path / "nested" / "cal_results" / "calibration_history.jsonl"
    assert Storage().append_history({"a": 1}) == target
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
        core = _core(connect_timeout=5.0)
        task = asyncio.create_task(cli._batch_gather_collect(core, lambda _m: None))
        await asyncio.sleep(0.05)  # the connect is in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stray = _no_stray_tasks()
        await asyncio.sleep(0.4)  # long enough for an orphaned connect to land
        return stray, dev.connected, dict(core.sessions)

    stray, connected, sessions = _run(run())
    assert stray and not connected and sessions == {}


def test_interrupted_collect_still_leaves_connected_sensors_releasable(monkeypatch):
    fleet = _fleet(2)
    _patch_fleet(monkeypatch, fleet)

    async def hang(_prompt):
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "ainput", hang)

    async def run():
        core = _core(connect_timeout=5.0)
        task = asyncio.create_task(cli._batch_gather_collect(core, lambda _m: None))
        for _ in range(100):
            if len(core.sessions) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        released = await cli._batch_release(core, lambda _m: None)
        return len(released), [d.connected for d in fleet.devices], _no_stray_tasks()

    assert _run(run()) == (2, [False, False], True)


# -- 7. main menu survives a failed action -----------------------------------------


def _menu_fleet(monkeypatch, choices: list[str]):
    fleet = _fleet(1)
    _patch_fleet(monkeypatch, fleet)
    picks = iter(choices)

    async def select(_message, _options):
        return next(picks)

    monkeypatch.setattr(cli._ui, "select", select)
    return fleet


def test_menu_reports_a_driver_error_and_returns_to_the_menu(monkeypatch, capsys):
    fleet = _menu_fleet(monkeypatch, ["3", "3", "q"])

    async def run():
        session = await _session(fleet.devices[0])
        real = session.ms.read_config
        calls = 0

        async def flaky(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise MS605TimeoutError("no response")
            return await real(*a, **kw)

        session.ms.read_config = flaky
        await cli.main_menu(session)
        await session.close()
        return calls

    assert _run(run()) == 2  # the second "read config" ran: the session survived
    out = capsys.readouterr().out
    assert "작업 실패" in out and "no response" in out


def test_menu_still_propagates_cancellation(monkeypatch):
    fleet = _menu_fleet(monkeypatch, ["3", "q"])

    async def run():
        session = await _session(fleet.devices[0])

        async def cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        session.ms.read_config = cancelled
        try:
            await cli.main_menu(session)
        finally:
            await session.close()

    with pytest.raises(asyncio.CancelledError):
        _run(run())


# -- 8. single-sensor writes go through the core pipeline ----------------------------


def test_set_zone_polls_through_the_apply_delay_and_leaves_a_snapshot(monkeypatch, capsys):
    fleet = _fleet(1, speed=2)  # the measured 1 s tag51 read-back lag, 0.5 s of wall time
    _patch_fleet(monkeypatch, fleet)
    dev = fleet.devices[0]
    pairs = [(140 - 10 * z, 70 - 5 * z) for z in range(7)]
    before = dev.thresholds

    async def run():
        session = await _session(dev)
        start = time.monotonic()
        await cli.flow_set_zone(session, pairs)
        elapsed = time.monotonic() - start
        snapshots = Storage().list_snapshots(session.device_id)
        await session.close()
        return elapsed, snapshots

    elapsed, snapshots = _run(run())
    out = capsys.readouterr().out
    assert "✅ 반영 확인됨." in out and "제한 시간 내 반영을 확인하지 못했습니다" not in out
    assert elapsed >= 0.45  # it waited for the lag instead of reading back once
    assert dev.thresholds == pairs
    (snap,) = snapshots
    assert snap.sections == ("zone_thresholds",) and snap.profile.zone_thresholds == before
