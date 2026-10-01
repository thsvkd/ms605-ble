"""ms605.events: EventBus dispatch, isolation, streams, and JSON-safe events.
Every value here is synthetic."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from ms605.events import (
    TERMINAL_CALIBRATION_STATES,
    ApplyResult,
    ApplyStatus,
    BatchChanged,
    BatchState,
    BusyChanged,
    CalibrationProgress,
    CalibrationResult,
    CalibrationState,
    CalibrationStateChanged,
    Event,
    EventBus,
    FrameDropped,
    GatherFailed,
    HandlerFailed,
    KeepAliveMissed,
    LinkState,
    LinkStateChanged,
    LiveRadar,
    PirChanged,
    SensorGathered,
)
from ms605.models import decode_radar_output

ADDR = "02:00:00:00:00:01"


def _busy(n: int) -> BusyChanged:
    return BusyChanged(address=ADDR, device_id=None, busy=str(n))


def test_callbacks_run_in_subscription_order_until_unsubscribed():
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda ev: seen.append("a"))
    off_b = bus.subscribe(lambda ev: seen.append("b"))
    bus.subscribe(lambda ev: seen.append("c"))
    bus.emit(_busy(1))
    off_b()
    off_b()  # a second call is a no-op
    bus.emit(_busy(2))
    assert seen == ["a", "b", "c", "a", "c"]


def test_unsubscribing_during_dispatch_skips_no_one():
    bus = EventBus()
    seen: list[str] = []

    def first(ev: Event) -> None:
        seen.append("first")
        off_first()

    off_first = bus.subscribe(first)
    bus.subscribe(lambda ev: seen.append("second"))
    bus.emit(_busy(1))
    bus.emit(_busy(2))
    assert seen == ["first", "second", "second"]


def test_emit_from_a_callback_dispatches_immediately():
    bus = EventBus()
    seen: list[str] = []

    def relay(ev: Event) -> None:
        seen.append(f"relay:{ev.busy}")
        if ev.busy == "1":
            bus.emit(_busy(2))

    bus.subscribe(relay)
    bus.subscribe(lambda ev: seen.append(f"tail:{ev.busy}"))
    bus.emit(_busy(1))
    # the nested event reaches everyone before the outer one reaches "tail"
    assert seen == ["relay:1", "relay:2", "tail:2", "tail:1"]


def test_a_failing_callback_emits_handler_failed_and_the_next_one_still_runs():
    bus = EventBus()
    seen: list[Event] = []

    def boom(ev: Event) -> None:
        if not isinstance(ev, HandlerFailed):
            raise RuntimeError("synthetic failure")

    bus.subscribe(boom)
    bus.subscribe(seen.append)
    bus.emit(_busy(1))
    failed = [ev for ev in seen if isinstance(ev, HandlerFailed)]
    assert len(failed) == 1
    assert failed[0].event_type == "BusyChanged"
    assert "boom" in failed[0].handler
    assert failed[0].error == "synthetic failure"
    assert any(isinstance(ev, BusyChanged) for ev in seen)


def test_a_callback_failing_on_handler_failed_does_not_recurse():
    bus = EventBus()
    calls: list[str] = []

    def always_fails(ev: Event) -> None:
        calls.append(type(ev).__name__)
        raise RuntimeError("synthetic failure")

    bus.subscribe(always_fails)
    bus.emit(_busy(1))
    assert calls == ["BusyChanged", "HandlerFailed"]


def test_stream_yields_events_and_stops_after_close():
    async def main() -> list[Event]:
        bus = EventBus()
        got: list[Event] = []
        async with bus.stream() as stream:
            bus.emit(_busy(1))
            bus.emit(_busy(2))
            got.append(await stream.__anext__())
            got.append(await stream.__anext__())
            waiter = asyncio.ensure_future(stream.__anext__())
            await asyncio.sleep(0)
            stream.close()
            with pytest.raises(StopAsyncIteration):
                await waiter
            bus.emit(_busy(3))  # unsubscribed: no longer queued
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
        return got

    got = asyncio.run(main())
    assert [ev.busy for ev in got] == ["1", "2"]


def test_full_stream_drops_the_oldest_event_and_counts_it():
    async def main():
        bus = EventBus()
        stream = bus.stream(maxsize=2)
        for n in range(5):
            bus.emit(_busy(n))
        got = [await stream.__anext__(), await stream.__anext__()]
        stream.close()
        return stream.dropped, [ev.busy for ev in got]

    dropped, kept = asyncio.run(main())
    assert dropped == 3
    assert kept == ["3", "4"]


def test_stream_async_for_ends_on_close():
    async def main() -> list[str]:
        bus = EventBus()
        stream = bus.stream()
        out: list[str] = []

        async def consume() -> None:
            async for ev in stream:
                out.append(ev.busy)

        task = asyncio.ensure_future(consume())
        bus.emit(_busy(1))
        await asyncio.sleep(0.01)
        stream.close()
        await asyncio.wait_for(task, 1.0)
        return out

    assert asyncio.run(main()) == ["1"]


def test_events_serialise_to_json():
    snapshot = decode_radar_output(bytes([0b001]) + bytes(70))
    events = [
        LinkStateChanged(address=ADDR, device_id="ab", state=LinkState.LOST, previous=LinkState.CONNECTED),
        LiveRadar(address=ADDR, device_id=None, snapshot=snapshot),
        CalibrationResult(
            address=ADDR,
            device_id="ab",
            state=CalibrationState.SUCCEEDED,
            started=True,
            before=((1, 2),) * 7,
            after=((3, 4),) * 7,
            error=None,
        ),
        KeepAliveMissed(address=ADDR, device_id=None, error="synthetic"),
        _busy(1),
        PirChanged(address=ADDR, device_id=None, detected=True),
        FrameDropped(address=ADDR, device_id=None, tag=55, reason="synthetic"),
        SensorGathered(
            address=ADDR, device_id="ab", name=None, known=False, site_id=None, alias=None, resolved_pending=False
        ),
        GatherFailed(address=ADDR, device_id=None, name="MRBL_SIM01", error="synthetic"),
        CalibrationStateChanged(
            address=ADDR, device_id="ab", state=CalibrationState.LEARNING, previous=CalibrationState.STARTING
        ),
        CalibrationProgress(address=ADDR, device_id="ab", elapsed_s=1.0, expected_s=180.0),
        BatchChanged(batch_id="0" * 32, state=BatchState.WAITING, fire_at=0.0, device_ids=("ab",)),
        ApplyResult(
            address=ADDR,
            device_id="ab",
            reason="apply",
            status=ApplyStatus.OK,
            applied=("zone_thresholds",),
            skipped=(),
            mismatched=(),
            snapshot=None,
            error=None,
        ),
        HandlerFailed(event_type="BusyChanged", handler="h", error="synthetic"),
    ]
    for ev in events:
        payload = json.loads(json.dumps({"type": type(ev).__name__, **asdict(ev)}))
        assert payload["type"] == type(ev).__name__
        assert isinstance(payload["at"], float)
    assert json.loads(json.dumps(asdict(events[0])))["state"] == "lost"


def test_enum_values_and_terminal_states():
    assert LinkState.CONNECTED.value == "connected"
    assert CalibrationState.TIMEOUT.value == "timeout"
    assert CalibrationState.IDLE not in TERMINAL_CALIBRATION_STATES
    assert TERMINAL_CALIBRATION_STATES == {
        CalibrationState.SUCCEEDED,
        CalibrationState.FAILED,
        CalibrationState.LOST,
        CalibrationState.TIMEOUT,
        CalibrationState.CANCELLED,
    }
