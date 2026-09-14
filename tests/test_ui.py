"""Offline checks for the ms605.cli._ui presentation layer -- specifically the
headless (non-TTY) fallbacks that keep every prompt working when questionary
cannot drive a full-screen widget (piped input, redirected logs, this test
suite). The interactive questionary path needs a real terminal and is verified
manually / via a pty harness, not here."""

from __future__ import annotations

import asyncio

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


def test_meter_fill_is_relative_to_threshold():
    # value == threshold fills exactly up to the tick
    bar = _ui.meter(25, 25, width=20, tick_at=6)
    assert bar.plain.count("█") == 6  # cells 0..5, tick at 6
    # value == 2x threshold fills to 2x the tick column
    bar2 = _ui.meter(50, 25, width=20, tick_at=6)
    assert bar2.plain[6] == "┃"
    assert bar2.plain.count("█") == 11  # 0..5 and 7..11 (cell 6 is the tick)


def test_meter_clamps_out_of_range_values():
    bar = _ui.meter(999, 10, width=16, tick_at=5)
    assert len(bar.plain) == 16  # no overflow past width


def test_meter_zero_threshold_is_safe():
    assert len(_ui.meter(5, 0, width=10, tick_at=3).plain) == 10
