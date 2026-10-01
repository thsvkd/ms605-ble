"""ms605.calibration against the protocol-level simulator: CalibrationJob's
state machine, cancellation, progress, history, and the presence preflight.
Every value here is synthetic."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ms605 import MS605ConnectionError, MS605TimeoutError
from ms605.calibration import CalibrationJob, build_calibration_record, presence_snapshot
from ms605.events import (
    CalibrationProgress,
    CalibrationResult,
    CalibrationState,
    CalibrationStateChanged,
    EventBus,
    LinkState,
    PirChanged,
)
from ms605.models import FALLBACK_DISTANCES_M, zone_distances
from ms605.protocol import SENSITIVITY_PRESETS, TAG_LIVE_OUTPUT_ENABLE, TAG_PIR_STATE, Sensitivity
from ms605.session import DeviceSession
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet
from ms605.storage import Storage

pytestmark = pytest.mark.usefixtures("no_chunk_pacing")

SPEED = 100.0
KEEPALIVE = 15 / SPEED
TIMEOUT = 200 / SPEED
MEDIUM = tuple(zip(*SENSITIVITY_PRESETS[Sensitivity.MEDIUM], strict=True))
S = CalibrationState


def _setup(**sim_kw):
    sim_kw.setdefault("speed", SPEED)
    sim_kw.setdefault("calibration_secs", 20)  # 0.2 s of wall time
    fleet = SimFleet(1, **sim_kw)
    dev = fleet.devices[0]
    dev.press_button()
    bus = EventBus()
    events: list = []
    bus.subscribe(events.append)
    session = DeviceSession(
        dev.ble_device, bus, scan=fleet.discover, client_factory=fleet.client_factory, keepalive_interval=KEEPALIVE
    )
    return dev, session, events


async def _connected(session: DeviceSession) -> None:
    await session.connect()
    await session.read_info()


def _states(events) -> list[CalibrationState]:
    return [e.state for e in events if isinstance(e, CalibrationStateChanged)]


def _results(events) -> list[CalibrationResult]:
    return [e for e in events if isinstance(e, CalibrationResult)]


async def _until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


# -- outcomes ------------------------------------------------------------------------


def test_success_records_before_after_history_and_progress(tmp_path):
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        storage = Storage(root=tmp_path)
        job = CalibrationJob(session, storage=storage, timeout=TIMEOUT, expected_s=0.2, progress_interval=0.02)
        result = await job.run()
        assert result is job.result is _results(events)[0] and len(_results(events)) == 1
        assert result.state is S.SUCCEEDED and job.state is S.SUCCEEDED
        assert result.started and result.error is None and result.history_saved and result.detail == ""
        assert result.before == MEDIUM
        assert result.after == tuple(DEFAULT_LEARNED_THRESHOLDS)
        assert result.device_id == session.device_id
        assert _states(events) == [S.STARTING, S.LEARNING, S.SUCCEEDED]

        progress = [e for e in events if isinstance(e, CalibrationProgress)]
        assert len(progress) >= 3
        assert all(e.expected_s == 0.2 for e in progress)
        assert [e.elapsed_s for e in progress] == sorted(e.elapsed_s for e in progress)
        assert events.index(progress[0]) > events.index(
            next(e for e in events if isinstance(e, CalibrationStateChanged) and e.state is S.LEARNING)
        )

        lines = storage.history_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["device_id"] == session.device_id
        assert [(z["trigger"], z["maintain"]) for z in record["zones"]] == list(DEFAULT_LEARNED_THRESHOLDS)
        assert session.busy is None and session.state is LinkState.CONNECTED
        with pytest.raises(RuntimeError):
            await job.run()
        await session.close()

    asyncio.run(main())


def test_device_reported_failure(tmp_path):
    async def main():
        dev, session, events = _setup(calibration_result=0)
        await _connected(session)
        storage = Storage(root=tmp_path)
        result = await CalibrationJob(session, storage=storage, timeout=TIMEOUT).run()
        assert result.state is S.FAILED and result.started
        assert result.error is None and result.detail == "device reported failure"
        assert result.after is None and not result.history_saved
        assert not storage.history_path.exists()
        assert dev.thresholds == list(MEDIUM)
        await session.close()

    asyncio.run(main())


def test_link_drop_while_learning_is_lost():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.ensure_future(job.run())
        await _until(lambda: job.state is S.LEARNING)
        dev.drop_link()
        result = await task
        assert result.state is S.LOST and result.started and result.error
        assert session.state is LinkState.LOST
        assert dev.thresholds == list(MEDIUM)  # learning reset, nothing stored

    asyncio.run(main())


def test_no_result_is_a_timeout():
    async def main():
        dev, session, events = _setup(calibration_secs=10_000)
        await _connected(session)
        result = await CalibrationJob(session, timeout=0.2).run()
        assert result.state is S.TIMEOUT and result.started
        assert _states(events) == [S.STARTING, S.LEARNING, S.TIMEOUT]
        await session.close()

    asyncio.run(main())


def test_refused_start_is_failed_with_error():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        dev.inject_status(1)  # the tag52=4 write is refused
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is S.FAILED and result.started
        assert "status 1" in result.error
        assert not dev.calibrating
        await session.close()

    asyncio.run(main())


def test_held_lock_is_failed_busy_without_io():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        async with session.operation("apply"):
            frames = len(dev.frames_in)
            result = await CalibrationJob(session, timeout=TIMEOUT).run()
            assert len(dev.frames_in) == frames
        assert result.state is S.FAILED and not result.started
        assert result.error == "busy: apply"
        await session.close()

    asyncio.run(main())


def test_not_connected_is_lost_not_started():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        session.keepalive_interval = 100.0
        await session.close()
        await session.connect()  # restarts the keep-alive with the long interval
        await _until(lambda: session.state is LinkState.LOST)  # idle drop
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is S.LOST and not result.started
        assert result.detail == "BLE link lost"
        assert result.before is None

    asyncio.run(main())


@pytest.mark.parametrize(
    ("exc", "state"),
    [(MS605ConnectionError("synthetic link loss"), S.LOST), (MS605TimeoutError("synthetic"), S.FAILED)],
)
def test_failed_pre_read_is_not_started(exc, state):
    async def main():
        dev, session, events = _setup()
        await _connected(session)

        async def failing_read(**_kw):
            raise exc

        session.ms.read_config = failing_read
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is state and not result.started
        assert result.error == str(exc) and result.before is None
        assert not dev.calibrating and session.busy is None
        await session.close()

    asyncio.run(main())


def test_history_write_failure_still_succeeds_with_detail(tmp_path):
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        (tmp_path / "calibration_history.jsonl").mkdir()  # appending to a directory fails
        result = await CalibrationJob(session, storage=Storage(root=tmp_path), timeout=TIMEOUT).run()
        assert result.state is S.SUCCEEDED and result.after == tuple(DEFAULT_LEARNED_THRESHOLDS)
        assert not result.history_saved and result.detail.startswith("결과 저장 실패")
        await session.close()

    asyncio.run(main())


def test_failed_read_back_still_succeeds_without_after():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        read = session.ms.read_config
        reads: list[int] = []

        async def second_read_fails(**kw):
            reads.append(1)
            if len(reads) == 2:
                raise MS605TimeoutError("synthetic")
            return await read(**kw)

        session.ms.read_config = second_read_fails
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is S.SUCCEEDED and result.after is None
        assert result.detail == "반영값 재조회 실패: synthetic" and not result.history_saved
        assert dev.thresholds == list(DEFAULT_LEARNED_THRESHOLDS)
        await session.close()

    asyncio.run(main())


# -- cancellation --------------------------------------------------------------------


def test_cancel_while_learning_drops_the_link_and_keeps_thresholds():
    async def main():
        dev, session, events = _setup(calibration_secs=10_000)
        await _connected(session)
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.ensure_future(job.run())
        await _until(lambda: job.state is S.LEARNING)
        await job.cancel()
        result = await task  # cancel() is not a cancellation of run()'s caller
        assert result.state is S.CANCELLED and result.started
        assert "버튼" in result.detail
        assert session.state is LinkState.DISCONNECTED and not dev.connected
        assert not dev.calibrating
        assert dev.thresholds == list(MEDIUM) == list(result.before)
        assert session.busy is None

    asyncio.run(main())


def test_external_task_cancel_does_the_same_and_reraises():
    async def main():
        dev, session, events = _setup(calibration_secs=10_000)
        await _connected(session)
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.ensure_future(job.run())
        await _until(lambda: job.state is S.LEARNING)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job.state is S.CANCELLED and job.result.started
        assert session.state is LinkState.DISCONNECTED and not dev.calibrating
        assert dev.thresholds == list(MEDIUM)

    asyncio.run(main())


def test_cancel_before_run_needs_no_io():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        frames = len(dev.frames_in)
        job = CalibrationJob(session, timeout=TIMEOUT)
        await job.cancel()
        assert job.state is S.CANCELLED and not job.result.started
        assert len(dev.frames_in) == frames
        assert session.state is LinkState.CONNECTED
        assert await job.run() is job.result  # the cancelled result, still no I/O
        assert len(dev.frames_in) == frames
        with pytest.raises(RuntimeError):
            await job.run()
        await session.close()

    asyncio.run(main())


def test_calibration_stream_reports_its_first_pir_value_even_if_unchanged():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        await session.acquire_live()
        dev._push([(TAG_PIR_STATE, b"\x00")])  # an earlier stream saw "no PIR"
        await _until(lambda: session.last_pir is False)
        await session.release_live()
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is S.SUCCEEDED
        changed = [e for e in events if isinstance(e, PirChanged)]
        assert [e.detected for e in changed] == [False, False]  # the device streams tag56 while learning
        await session.close()

    asyncio.run(main())


def test_cancel_before_a_run_task_starts_returns_the_cancelled_result():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        frames = len(dev.frames_in)
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.create_task(job.run())  # not started yet
        await job.cancel()
        result = await task
        assert result is job.result and result.state is S.CANCELLED and not result.started
        assert len(dev.frames_in) == frames and session.state is LinkState.CONNECTED
        assert len(_results(events)) == 1
        await session.close()

    asyncio.run(main())


def test_cancel_during_the_pre_read_sends_no_start():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        read = session.ms.read_config
        reading = asyncio.Event()

        async def slow_read(**kw):
            reading.set()
            await asyncio.sleep(0.05)
            return await read(**kw)

        session.ms.read_config = slow_read
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.ensure_future(job.run())
        await reading.wait()
        await job.cancel()
        result = await task
        assert result.state is S.CANCELLED and not result.started
        assert not dev.calibrating and session.state is LinkState.CONNECTED
        assert _states(events) == [S.CANCELLED]
        await session.close()

    asyncio.run(main())


def test_task_cancel_after_learning_succeeded_reports_success():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        read = session.ms.read_config
        reads: list[int] = []
        reading_back = asyncio.Event()

        async def hanging_read_back(**kw):
            reads.append(1)
            if len(reads) == 2:
                reading_back.set()
                await asyncio.sleep(60)
            return await read(**kw)

        session.ms.read_config = hanging_read_back
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.ensure_future(job.run())
        await asyncio.wait_for(reading_back.wait(), 2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job.state is S.SUCCEEDED and job.result.after is None
        assert "cancelled" in job.result.detail
        assert session.busy is None and session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


# -- keep-alive hand-over --------------------------------------------------------------


def test_session_ping_pauses_during_calibration_and_resumes_at_once():
    async def main():
        dev, session, events = _setup(calibration_secs=50)  # 0.5 s: several keep-alive periods
        await _connected(session)
        job = CalibrationJob(session, timeout=TIMEOUT)
        loop = asyncio.get_running_loop()
        pings: list[tuple[str, CalibrationState, float]] = []
        ping = session.ms.ping

        async def recording_ping(**kw):
            pings.append((asyncio.current_task().get_coro().__qualname__, job.state, loop.time()))
            await ping(**kw)

        session.ms.ping = recording_ping
        ended: list[float] = []  # the result is emitted just before the lock is released
        session.bus.subscribe(lambda ev: isinstance(ev, CalibrationResult) and ended.append(loop.time()))
        await job.run()
        finished = ended[0]
        assert job.state is S.SUCCEEDED
        during = [who for who, state, _ in pings if state is S.LEARNING]
        assert during and all("DeviceSession" not in who for who in during)  # only the driver's
        await _until(lambda: any(t >= finished and "DeviceSession" in who for who, _, t in pings))
        first_after = min(t for who, _, t in pings if t >= finished and "DeviceSession" in who)
        assert first_after - finished < KEEPALIVE / 2  # immediate, not a full interval later
        assert session.state is LinkState.CONNECTED
        await session.close()

    asyncio.run(main())


def test_session_pings_the_moment_calibration_releases_the_lock():
    async def main():
        dev, session, events = _setup(idle_timeout=None)
        session.keepalive_interval = 60.0  # no scheduled ping can land during this test
        await _connected(session)
        loop = asyncio.get_running_loop()
        session_pings: list[float] = []
        ping = session.ms.ping

        async def recording_ping(**kw):
            if "DeviceSession" in asyncio.current_task().get_coro().__qualname__:
                session_pings.append(loop.time())
            await ping(**kw)

        session.ms.ping = recording_ping
        result = await CalibrationJob(session, timeout=TIMEOUT).run()
        assert result.state is S.SUCCEEDED and session_pings == []
        await _until(lambda: session_pings, timeout=1.0)  # the kick on release, not the 60 s interval
        await session.close()

    asyncio.run(main())


def _radar_after(events, index: int) -> int:
    return sum(1 for e in events[index:] if type(e).__name__ == "LiveRadar")


def test_live_viewer_that_joined_during_calibration_gets_live_output_afterwards():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.create_task(job.run())
        await _until(lambda: job.state is S.LEARNING)
        await session.acquire_live()  # tag54 is left alone while calibrating
        assert _live_writes(dev) == []
        result = await task
        assert result.state is S.SUCCEEDED and session.state is LinkState.CONNECTED
        assert _live_writes(dev) == [1]  # written once the calibration lock was released
        mark = len(events)
        await asyncio.sleep(0.05)
        assert _radar_after(events, mark) >= 2
        await session.release_live()
        await session.close()

    asyncio.run(main())


def test_live_viewer_that_left_during_calibration_turns_live_output_off_afterwards():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        await session.acquire_live()
        job = CalibrationJob(session, timeout=TIMEOUT)
        task = asyncio.create_task(job.run())
        await _until(lambda: job.state is S.LEARNING)
        await session.release_live()
        assert _live_writes(dev) == [1]
        assert (await task).state is S.SUCCEEDED
        assert _live_writes(dev) == [1, 0]
        mark = len(events)
        await asyncio.sleep(0.05)
        assert _radar_after(events, mark) == 0
        await session.close()

    asyncio.run(main())


def test_driver_on_started_fires_once_after_the_ack_and_its_errors_are_only_logged(caplog):
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        calls: list[bool] = []

        def boom() -> None:
            calls.append(dev.calibrating)  # the tag52=4 write was already applied
            raise RuntimeError("synthetic hook failure")

        async with session.operation("calibration", suspend_keepalive=True) as ms:
            ok = await ms.start_auto_calibration(timeout=TIMEOUT, keepalive_interval=KEEPALIVE, on_started=boom)
        assert ok and calls == [True]
        await session.close()

    asyncio.run(main())
    assert "on_started hook failed" in caplog.text


# -- preflight -------------------------------------------------------------------------


def _live_writes(dev) -> list[int]:
    return [v[0] for f in dev.frames_in for t, v in f.attributes if t == TAG_LIVE_OUTPUT_ENABLE]


def test_preflight_sees_presence():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        dev.signal = [(500, 500)] * 7
        snap = await presence_snapshot(session, window_s=0.05)
        assert snap.samples >= 2 and snap.presence is True and snap.occupied is True
        assert snap.device_id == session.device_id and snap.error is None
        assert _live_writes(dev) == [1, 0]  # live output released afterwards
        await session.close()

    asyncio.run(main())


def test_preflight_absent_room():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        snap = await presence_snapshot(session, window_s=0.05)
        assert snap.samples >= 2
        assert (snap.presence, snap.pir, snap.occupied) == (False, None, False)
        await session.close()

    asyncio.run(main())


def test_preflight_counts_pir_as_occupied():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        await session.acquire_live()  # e.g. a monitor screen is open: the stream is current
        dev._push([(TAG_PIR_STATE, b"\x01")])
        await _until(lambda: session.last_pir is True)
        snap = await presence_snapshot(session, window_s=0.05)
        assert (snap.presence, snap.pir, snap.occupied) == (False, True, True)
        await session.release_live()
        await session.close()

    asyncio.run(main())


def test_preflight_ignores_a_pir_value_from_an_earlier_stream():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        await session.acquire_live()
        dev._push([(TAG_PIR_STATE, b"\x01")])
        await _until(lambda: session.last_pir is True)
        await session.release_live()  # that stream ended; the PIR may have changed since
        snap = await presence_snapshot(session, window_s=0.05)
        assert (snap.presence, snap.pir, snap.occupied) == (False, None, False)
        await session.close()

    asyncio.run(main())


def test_preflight_sees_a_pir_change_inside_the_window():
    async def main():
        dev, session, events = _setup(live_interval=1_000)  # no tag55: only the PIR can say "occupied"
        await _connected(session)
        assert session.last_pir is None
        loop = asyncio.get_running_loop()
        loop.call_later(0.02, dev._push, [(TAG_PIR_STATE, b"\x01")])
        snap = await presence_snapshot(session, window_s=0.1)
        assert (snap.samples, snap.presence, snap.pir, snap.occupied) == (0, None, True, True)
        await session.close()

    asyncio.run(main())


def test_preflight_without_samples_is_unknown():
    async def main():
        dev, session, events = _setup(live_interval=1_000)
        await _connected(session)
        snap = await presence_snapshot(session, window_s=0.05)
        assert (snap.samples, snap.presence, snap.pir, snap.occupied) == (0, None, None, None)
        await session.close()
        with pytest.raises(MS605ConnectionError):
            await presence_snapshot(session, window_s=0.05)

    asyncio.run(main())


# -- history record --------------------------------------------------------------------


def test_calibration_record_keeps_the_old_keys_and_adds_device_id_last():
    async def main():
        dev, session, events = _setup()
        await _connected(session)
        async with session.operation("read") as ms:
            cfg = await ms.read_config()
        await session.close()
        return cfg

    cfg = asyncio.run(main())
    record = build_calibration_record("MRBL_SIM01", "00000000-0000-4000-8000-000000000001", cfg, device_id="01")
    assert list(record) == [
        "timestamp",
        "device_name",
        "device_address",
        "sensitivity",
        "detect_mode",
        "zones",
        "device_id",
    ]
    assert record["device_id"] == "01" and len(record["zones"]) == 7
    assert record["zones"][0]["distance_m"] == pytest.approx(0.8)
    assert zone_distances(cfg) == pytest.approx((0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6))  # 8 edges -> 7 far edges
    assert zone_distances(replace(cfg, zone_distances_m=None)) == FALLBACK_DISTANCES_M
