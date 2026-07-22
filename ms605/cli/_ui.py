"""ms605.cli._ui -- the presentation layer for the interactive CLI.

A thin, centralised wrapper over `rich` (styled output: panels, tables,
spinners, colour) and `questionary` (modern keyboard-driven prompts: arrow-key
select, space-toggle checkbox, y/N confirm) so every flow in cli.py shares one
look and one interaction model.

Two invariants keep this safe to sprinkle everywhere:

* **Non-TTY fallback.** When stdin/stdout is not a terminal (piped input, a
  redirected log, the test suite, unattended `--schedule` runs), questionary
  cannot drive a full-screen prompt -- so every widget degrades to a plain
  numbered/`ainput()` prompt that still works headless. `rich` itself already
  auto-disables colour/animation off a TTY.

* **Interrupt parity.** Widgets use questionary's ``unsafe_ask_async()``, which
  lets Ctrl-C raise KeyboardInterrupt instead of swallowing it -- so the hard
  exit in cli.main still fires and Ctrl-C quits from any prompt (matching the
  daemon-thread ``ainput`` behaviour it sits alongside)."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import questionary
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


# --- low-level line input (owned here so _shared and _ui share one primitive) -
def _prompt(text_: str) -> str:
    try:
        return input(text_)
    except EOFError:
        return ""


async def ainput(prompt: str = "") -> str:
    """Async input() that stays cancellable -- so Ctrl-C works at any prompt.

    A blocking readline() cannot be interrupted, and asyncio's default
    run_in_executor would leave that read parked on the shared thread pool; on
    Ctrl-C, asyncio.run's shutdown then blocked joining the still-reading
    worker, so the interrupt only landed after an extra Enter.

    The read runs on a throwaway *daemon* thread that resolves a plain
    asyncio.Future instead. Cancelling the await (task cancellation /
    KeyboardInterrupt) completes immediately and abandons the daemon thread --
    daemon threads never block interpreter exit, and the process hard-exits
    (see cli.main) which reaps it. The event loop keeps running throughout, so
    notify callbacks / keep-alives still fire while we wait for input."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    def _deliver(setter, payload) -> None:
        def _apply() -> None:
            if not result.done():
                setter(payload)

        try:
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            pass  # loop already closing on shutdown; nothing to deliver

    def _worker() -> None:
        try:
            value = _prompt(prompt)
        except BaseException as exc:  # noqa: BLE001 - hand any failure to the awaiter
            _deliver(result.set_exception, exc)
        else:
            _deliver(result.set_result, value)

    threading.Thread(target=_worker, name="ms605-stdin", daemon=True).start()
    return await result

# --- theme ----------------------------------------------------------------
_ACCENT = "#00d7af"  # teal -- the one brand colour, reused by rich + questionary

_THEME = Theme(
    {
        "brand": f"bold {_ACCENT}",
        "accent": _ACCENT,
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "muted": "grey58",
        "key": "bold cyan",
        "addr": "grey62",
        # live-monitor bar meters
        "bar.fill": "green",
        "bar.active": "bold red",
        "bar.tick": "bold yellow",
        "bar.empty": "grey30",
    }
)
console = Console(theme=_THEME, highlight=False)

# questionary styling, matched to the rich theme above.
_QS_STYLE = questionary.Style(
    [
        ("qmark", f"fg:{_ACCENT} bold"),
        ("question", "bold"),
        ("answer", f"fg:{_ACCENT} bold"),
        ("pointer", f"fg:{_ACCENT} bold"),
        ("highlighted", f"fg:{_ACCENT} bold"),
        ("selected", f"fg:{_ACCENT}"),
        ("separator", "fg:grey"),
        ("instruction", "fg:grey"),
        ("text", ""),
        ("disabled", "fg:grey italic"),
    ]
)


def interactive() -> bool:
    """True only when both ends are a real terminal -- the gate for every
    questionary widget (they fall back to plain prompts otherwise)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, OSError):  # closed/detached streams
        return False


# --- styled output --------------------------------------------------------
def header(title: str, subtitle: str | None = None) -> None:
    """A framed brand header -- the top of each screen/flow."""
    body = Text(title, style="brand")
    if subtitle:
        body.append(f"\n{subtitle}", style="muted")
    console.print(Panel(body, border_style="accent", padding=(0, 2)))


def rule(text: str) -> None:
    console.rule(f"[brand]{text}[/]", style="accent")


def info(msg: str) -> None:
    console.print(msg)


def muted(msg: str) -> None:
    console.print(msg, style="muted")


def success(msg: str) -> None:
    console.print(f"[ok]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/] {msg}")


def error(msg: str) -> None:
    console.print(f"[err]✗[/] {msg}")


def panel(renderable: Any, *, title: str | None = None, style: str = "accent") -> None:
    console.print(Panel(renderable, title=title, border_style=style, padding=(0, 2)))


@contextmanager
def status(msg: str):
    """Spinner while a slow op (scan/connect) runs; a no-op line off a TTY."""
    if interactive():
        with console.status(f"[accent]{msg}[/]", spinner="dots"):
            yield
    else:
        console.print(msg, style="muted")
        yield


# --- interactive widgets (questionary, with headless fallbacks) -----------
def _choices(options: Sequence[tuple[str, Any]]) -> list[questionary.Choice]:
    return [questionary.Choice(title=title, value=value) for title, value in options]


async def select(
    message: str,
    options: Sequence[tuple[str, Any]],
    *,
    default: Any = None,
) -> Any:
    """Single choice from `options` (list of (label, value)). Arrow keys on a
    TTY; a numbered `ainput` prompt otherwise. Returns the chosen value."""
    if not options:
        return None
    if interactive():
        default_choice = None
        if default is not None:
            default_choice = next((c for c in _choices(options) if c.value == default), None)
        return await questionary.select(
            message,
            choices=_choices(options),
            default=default_choice,
            style=_QS_STYLE,
            qmark="?",
            instruction="(↑/↓ 이동, Enter 선택)",
            use_shortcuts=False,
        ).unsafe_ask_async()

    # headless fallback
    console.print(message, style="brand")
    for i, (label, _) in enumerate(options):
        console.print(f"  [key]{i}[/] {label}")
    raw = (await ainput("번호 선택: ")).strip()
    if raw.isdigit() and 0 <= int(raw) < len(options):
        return options[int(raw)][1]
    return default if default is not None else options[0][1]


async def checkbox(
    message: str,
    options: Sequence[tuple[str, Any]],
    *,
    checked: Sequence[Any] | None = None,
) -> list[Any] | None:
    """Multi-select from `options`; items in `checked` (default: all) start
    ticked. Space toggles, Enter confirms on a TTY; a space-separated numbered
    `ainput` prompt otherwise (Enter = keep all checked). Returns chosen values
    (possibly empty), or None if the operator explicitly cancels the fallback."""
    if not options:
        return []
    checked_set = set(checked) if checked is not None else {v for _, v in options}
    if interactive():
        qchoices = [
            questionary.Choice(title=label, value=value, checked=value in checked_set)
            for label, value in options
        ]
        return await questionary.checkbox(
            message,
            choices=qchoices,
            style=_QS_STYLE,
            qmark="?",
            instruction="(↑/↓ 이동, Space 토글, Enter 확정)",
        ).unsafe_ask_async()

    # headless fallback
    console.print(message, style="brand")
    for i, (label, value) in enumerate(options):
        mark = "x" if value in checked_set else " "
        console.print(f"  [key]{i}[/] [{mark}] {label}")
    raw = (await ainput("번호 공백구분 (Enter=체크된 전체, 'c'=취소): ")).strip().lower()
    if raw == "c":
        return None
    if raw == "":
        return [v for _, v in options if v in checked_set]
    try:
        idxs = sorted({int(x) for x in raw.split()})
    except ValueError:
        return [v for _, v in options if v in checked_set]
    if any(not (0 <= i < len(options)) for i in idxs):
        return [v for _, v in options if v in checked_set]
    return [options[i][1] for i in idxs]


async def confirm(message: str, *, default: bool = False) -> bool:
    """y/N confirm. Arrow-free single-key on a TTY; `ainput` otherwise."""
    if interactive():
        return bool(
            await questionary.confirm(
                message, default=default, style=_QS_STYLE, qmark="?"
            ).unsafe_ask_async()
        )
    suffix = "[Y/n]" if default else "[y/N]"
    raw = (await ainput(f"{message} {suffix}: ")).strip().lower()
    if raw == "":
        return default
    return raw in ("y", "yes")


async def text(message: str, *, default: str = "") -> str:
    """Free-form line input (threshold values, HH:MM, ...)."""
    if interactive():
        return await questionary.text(
            message, default=default, style=_QS_STYLE, qmark="?"
        ).unsafe_ask_async()
    raw = (await ainput(f"{message} ")).strip()
    return raw if raw else default


# --- reusable rich tables -------------------------------------------------
def zone_threshold_table(
    rows: Sequence[tuple[int, str, int, int]],
    *,
    title: str | None = None,
    marks: Sequence[tuple[bool, bool]] | None = None,
) -> Table:
    """A styled per-zone Trigger/Maintain table. `rows` is (index, distance,
    trigger, maintain); optional `marks` flags a changed column with '*'."""
    table = Table(title=title, title_style="brand", header_style="key", border_style="muted")
    table.add_column("#", justify="right")
    table.add_column("거리", justify="right")
    table.add_column("Trigger", justify="right")
    table.add_column("Maintain", justify="right")
    for i, (idx, dist, trig, maint) in enumerate(rows):
        t = f"{trig}"
        m = f"{maint}"
        if marks is not None and i < len(marks):
            mt, mm = marks[i]
            if mt:
                t = f"[ok]{trig}*[/]"
            if mm:
                m = f"[ok]{maint}*[/]"
        table.add_row(str(idx), dist, t, m)
    return table


# --- live monitor (real-time bar meters) ----------------------------------
def meter(value: int, threshold: int, *, scale: float, width: int = 24, active: bool = False) -> Text:
    """A horizontal bar for `value` on a 0..`scale` axis, with a tick at
    `threshold`. Fill turns red while the zone is trigger-active. Values are
    clamped into range (radar energy is small and non-negative in normal use)."""
    scale = max(float(scale), 1.0)
    filled = max(0, min(width, round(width * value / scale)))
    tick = max(0, min(width - 1, round(width * threshold / scale)))
    fill_style = "bar.active" if active else "bar.fill"
    bar = Text()
    for i in range(width):
        if i == tick:
            bar.append("┃", style="bar.tick")
        elif i < filled:
            bar.append("█", style=fill_style)
        else:
            bar.append("─", style="bar.empty")
    return bar


def make_live(renderable: Any):
    """A transient, manually-refreshed Live region for the real-time monitor --
    updates are driven by device pushes (live.update(..., refresh=True)), not a
    background clock, and the region is cleared on exit (transient)."""
    return Live(renderable, console=console, auto_refresh=False, transient=True)
