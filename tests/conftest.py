"""Shared test fixtures."""

from __future__ import annotations

import pytest

from ms605.driver import MS605


@pytest.fixture
def no_chunk_pacing(monkeypatch):
    """The driver paces BLE chunks 20 ms apart in wall time; the simulator needs
    no pacing. Core objects build their own MS605 (no inter_chunk_delay knob),
    so drop the default for the test instead -- the suite stays fast."""
    monkeypatch.setitem(MS605.__init__.__kwdefaults__, "inter_chunk_delay", 0)
