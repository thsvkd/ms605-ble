"""The M1 completion path end to end on the simulator, through the core only
(no CLI): gather -> batch calibration -> clone. Plus the static check that the
core never writes to the terminal (docs/CORE_API.md 2, 14). Every value here is
synthetic."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import ms605
from ms605.events import ApplyStatus, CalibrationState, LinkState, SensorGathered
from ms605.fleet import Draft, Fleet, SensorChanges, ThresholdChange
from ms605.models import ConfigProfile
from ms605.registry import Registry
from ms605.sim import DEFAULT_LEARNED_THRESHOLDS, SimFleet
from ms605.storage import Storage

pytestmark = pytest.mark.usefixtures("no_chunk_pacing")

SPEED = 100.0


def test_gather_calibrate_then_clone_on_a_sim_fleet(tmp_path):
    async def main():
        # read-back lag off: the polled verify has its own tests (test_fleet); this is the path
        sim = SimFleet(3, speed=SPEED, calibration_secs=20, apply_delay=None)
        storage = Storage(root=tmp_path)
        events: list = []
        async with Fleet(
            Registry(storage, host="host-1"),
            storage,
            scan=sim.discover,
            client_factory=sim.client_factory,
            keepalive_interval=15 / SPEED,
            gather_pause=0.01,
        ) as fleet:
            fleet.bus.subscribe(events.append)

            # 1. gather: every sensor whose button is pressed joins
            fleet.start_gather()
            for dev in sim.devices:
                dev.press_button()
            while len(fleet.sessions) < 3:
                await asyncio.sleep(0.005)
            await fleet.stop_gather()
            ids = list(fleet.sessions)
            assert len([e for e in events if isinstance(e, SensorGathered)]) == 3

            # 2. calibrate them together
            results = await fleet.calibrate(ids, timeout=200 / SPEED).wait()
            assert all(results[i].state is CalibrationState.SUCCEEDED for i in ids)
            assert all(results[i].after == tuple(DEFAULT_LEARNED_THRESHOLDS) for i in ids)
            assert sorted(r["device_id"] for r in storage.read_history()) == sorted(ids)

            # 3. tune the first sensor, then clone it onto the other two
            source, targets = ids[0], ids[1:]
            tune = Draft([source], bulk=SensorChanges(zone_thresholds=ThresholdChange(True, [5] * 7, [None] * 7)))
            assert (await fleet.apply(tune))[source].status is ApplyStatus.OK
            async with fleet.sessions[source].operation("read") as ms:
                profile = ConfigProfile.from_config(await ms.read_config())
            clone = Draft(targets, bulk=SensorChanges.from_profile(profile, ["sensitivity", "zone_thresholds"]))
            applied = await fleet.apply(clone)
            assert all(applied[i].status is ApplyStatus.OK and applied[i].snapshot for i in targets)
            expected = [(t + 5, m) for t, m in DEFAULT_LEARNED_THRESHOLDS]
            assert [d.thresholds for d in sim.devices] == [expected] * 3
            sessions = list(fleet.sessions.values())

        assert all(s.state is LinkState.DISCONNECTED for s in sessions)
        assert not any(d.connected for d in sim.devices)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(main())


_FORBIDDEN = re.compile(r"\bprint\(|\brich\b|\bquestionary\b|ms605\.cli|from \.cli\b|from \. import cli\b")


def test_core_modules_never_write_to_the_terminal():
    core = sorted(p for p in Path(ms605.__file__).parent.glob("*.py"))
    assert {"session.py", "fleet.py", "calibration.py", "events.py"} <= {p.name for p in core}
    offenders = [
        f"{p.name}:{n}: {line.strip()}"
        for p in core
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _FORBIDDEN.search(line)
    ]
    assert offenders == []
