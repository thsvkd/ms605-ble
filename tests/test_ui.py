"""Offline checks for the ms605.cli._ui presentation layer -- specifically the
headless (non-TTY) fallbacks that keep every prompt working when questionary
cannot drive a full-screen widget (piped input, redirected logs, this test
suite). The interactive questionary path needs a real terminal and is verified
manually / via a pty harness, not here."""

from __future__ import annotations

import asyncio

import pytest

from ms605.cli import _ui


def _run(coro):
    return asyncio.run(coro)


def _feed(monkeypatch, *answers):
    """Force the headless path and script _ui.ainput to return `answers` in order."""
    monkeypatch.setattr(_ui, "interactive", lambda: False)
    it = iter(answers)

    async def fake_ainput(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            return ""

    monkeypatch.setattr(_ui, "ainput", fake_ainput)


def test_select_fallback_parses_index(monkeypatch):
    _feed(monkeypatch, "1")
    got = _run(_ui.select("pick", [("A", "a"), ("B", "b"), ("C", "c")]))
    assert got == "b"


def test_select_fallback_bad_input_uses_default(monkeypatch):
    _feed(monkeypatch, "nonsense")
    got = _run(_ui.select("pick", [("A", "a"), ("B", "b")], default="b"))
    assert got == "b"


def test_checkbox_fallback_parses_indices(monkeypatch):
    _feed(monkeypatch, "0 2")
    got = _run(_ui.checkbox("multi", [("X", "x"), ("Y", "y"), ("Z", "z")], checked=[]))
    assert got == ["x", "z"]


def test_checkbox_fallback_empty_keeps_checked_default(monkeypatch):
    _feed(monkeypatch, "")  # Enter -> keep the pre-checked set
    got = _run(_ui.checkbox("multi", [("X", "x"), ("Y", "y"), ("Z", "z")], checked=["x", "z"]))
    assert got == ["x", "z"]


def test_checkbox_fallback_cancel_returns_none(monkeypatch):
    _feed(monkeypatch, "c")
    got = _run(_ui.checkbox("multi", [("X", "x"), ("Y", "y")]))
    assert got is None


def test_confirm_fallback_yes_no_and_default(monkeypatch):
    _feed(monkeypatch, "y")
    assert _run(_ui.confirm("go?", default=False)) is True
    _feed(monkeypatch, "n")
    assert _run(_ui.confirm("go?", default=True)) is False
    _feed(monkeypatch, "")  # Enter -> default
    assert _run(_ui.confirm("go?", default=True)) is True


def test_text_fallback_returns_input_or_default(monkeypatch):
    _feed(monkeypatch, "hello")
    assert _run(_ui.text("value")) == "hello"
    _feed(monkeypatch, "")
    assert _run(_ui.text("value", default="fallback")) == "fallback"


def test_zone_threshold_table_marks_changed_columns():
    table = _ui.zone_threshold_table(
        [(0, "0.8 m", 95, 40), (1, "1.6 m", 85, 40)],
        title="존 임계값",
        marks=[(True, False), (False, True)],
    )
    assert table.row_count == 2
    assert table.title == "존 임계값"


def test_select_empty_options_returns_none():
    assert _run(_ui.select("nothing", [])) is None


def test_meter_tick_is_at_a_fixed_column_regardless_of_threshold():
    # the whole point of the fix: the tick sits at `tick_at` no matter the
    # threshold value, so it never drifts frame-to-frame.
    for thr in (10, 55, 200):
        bar = _ui.meter(thr, thr, width=20, tick_at=6)
        assert bar.plain[6] == "┃"
        assert len(bar.plain) == 20


def _over(bar, tick):
    """Cells filled past the tick (the over-threshold part)."""
    return bar.plain[tick + 1:].count("█")


def _under(bar, tick):
    """Cells filled before the tick."""
    return bar.plain[:tick].count("█")


def _is_red(bar):
    return any(span.style == "bar.active" for span in bar.spans)


@pytest.mark.parametrize("thr", [60, 1, 0, -33])
def test_meter_crosses_tick_iff_over_threshold(thr):
    # Holds for positive, zero and negative (calibration) thresholds alike.
    at = _ui.meter(thr, thr, width=16, tick_at=5)
    assert (_under(at, 5), _over(at, 5)) == (5, 0)  # fills exactly to the tick
    above = _ui.meter(thr + 1, thr, width=16, tick_at=5)
    assert _under(above, 5) == 5 and _over(above, 5) >= 1
    below = _ui.meter(thr - 1, thr, width=16, tick_at=5)
    assert _under(below, 5) < 5 and _over(below, 5) == 0


def test_meter_distinguishes_near_threshold_values():
    # Regression: 55/59/60/70/77 vs 60 used to all render identically.
    bars = [_ui.meter(v, 60, width=16, tick_at=5).plain for v in (55, 59, 60, 70, 77)]
    assert bars[0][:5] != bars[2][:5] and bars[1][:5] != bars[2][:5]  # under: short of tick
    assert _over(_ui.meter(70, 60, width=16, tick_at=5), 5) >= 1
    assert _over(_ui.meter(77, 60, width=16, tick_at=5), 5) > _over(_ui.meter(61, 60, width=16, tick_at=5), 5)


def test_meter_negative_threshold_shows_value_above_it():
    # Regression: meter(-10, -33) and meter(0, -33) used to draw an empty bar.
    for v in (-10, 0):
        bar = _ui.meter(v, -33, width=16, tick_at=5)
        assert _under(bar, 5) == 5 and _over(bar, 5) >= 1
        assert _is_red(bar)


def test_meter_zero_threshold_does_not_saturate_small_values():
    bar = _ui.meter(5, 0, width=16, tick_at=5)
    assert 1 <= _over(bar, 5) < 10


def test_meter_fill_colour_follows_value_vs_threshold():
    assert _is_red(_ui.meter(61, 60, width=16, tick_at=5))
    assert not _is_red(_ui.meter(60, 60, width=16, tick_at=5))
    assert not _is_red(_ui.meter(0, 0, width=16, tick_at=5))


def test_meter_clamps_out_of_range_values():
    hi = _ui.meter(999, 10, width=16, tick_at=5)
    assert hi.plain == "█████┃██████████"
    lo = _ui.meter(-999, 10, width=16, tick_at=5)
    assert lo.plain == "─────┃──────────"


@pytest.mark.parametrize("width,tick_at", [(1, 0), (10, 0), (10, 9), (10, 10)])
def test_meter_rejects_tick_outside_bar(width, tick_at):
    with pytest.raises(ValueError):
        _ui.meter(1, 1, width=width, tick_at=tick_at)
