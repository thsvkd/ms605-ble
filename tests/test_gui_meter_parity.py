"""The web meter (web/src/meter.ts) and the CLI meter (ms605.cli._ui.meter) read
the same reference cases: every case must hold for the CLI, so the TS test that
reads the same file checks the port against the CLI (docs/GUI_API.md 14.8.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ms605.cli._ui import meter

CASES_JSON = Path(__file__).resolve().parent.parent / "web" / "src" / "test" / "meter_cases.json"
CASES = json.loads(CASES_JSON.read_text(encoding="utf-8"))


def test_the_cases_cover_zero_and_negative_thresholds():
    assert len(CASES) >= 20
    assert any(c["threshold"] == 0 for c in CASES)
    assert any(c["threshold"] < 0 for c in CASES)
    assert {c["over"] for c in CASES} == {True, False}


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['value']}/{c['threshold']}@{c['width']},{c['tick_at']}")
def test_cli_meter_matches_the_shared_case(case):
    bar = meter(case["value"], case["threshold"], width=case["width"], tick_at=case["tick_at"])
    assert bar.plain == case["plain"]
    assert any(span.style == "bar.active" for span in bar.spans) is case["over"]
