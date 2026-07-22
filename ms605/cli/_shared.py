"""ms605.cli._shared -- helpers shared by the MS605 CLIs.

`discover_and_select`/`connect_with_retry`/`LiveLink` implement the
single-device, button-press-aware connect flow used by the interactive app and
one-shot subcommands (and by tools/debug_zone_write.py) -- not by the
`ms605 calibrate` batch path, which manages several devices at once with its
own pooling logic (see run_batch_calibration in cli.py).

The low-level `ainput` primitive lives in ms605.cli._ui (the presentation
layer) and is re-exported here for the callers that still import it from
_shared; new prompts should use the styled _ui.select/checkbox/confirm widgets.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ms605 import MS605, MS605Error
from ms605.cli import _ui
from ms605.cli._ui import ainput
from ms605.discovery import friendly_ble_error

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

__all__ = ["ainput", "LiveLink", "connect_with_retry", "discover_and_select"]


async def discover_and_select(
    scan_secs: float,
    *,
    auto_select: bool = False,
    prefer_address: str | None = None,
) -> BLEDevice:
    """Scan and pick an MS605; rescans automatically until a match is found
    (press the button to make it advertise).

    Selection priority:
    1. `prefer_address` -- if given (the address/UUID printed on a previous run,
       or the device name), the moment that exact device appears it is chosen
       immediately, even among several devices; the menu and RSSI auto-select are
       skipped and scanning keeps going until *that* device shows up.
    2. `auto_select` (one-shot subcommands) -- if exactly one device is
       advertising, pick it with no prompt; if several are, fall back to the
       numbered menu so the user still chooses.
    3. otherwise -- show a numbered menu and let the user choose or rescan."""
    want = prefer_address.strip().lower() if prefer_address else None

    def _is_target(dev: BLEDevice) -> bool:
        return want is not None and (
            dev.address.lower() == want or (dev.name or "").lower() == want
        )

    attempt = 0
    while True:
        attempt += 1
        with _ui.status(f"MS605 검색 중... (스캔 {scan_secs:.0f}s, 시도 {attempt})"):
            devices = await MS605.scan(timeout=scan_secs)

        # 1) a specific device was requested: take it the instant it appears,
        #    no matter how many others are around; otherwise keep scanning.
        if want is not None:
            for dev in devices:
                if _is_target(dev):
                    _ui.success(f"지정 기기 자동 선택: [brand]{dev.name or '(이름없음)'}[/] [addr]{dev.address}[/]")
                    return dev
            if devices:
                _ui.muted(f"발견된 {len(devices)}개 중 지정 기기({prefer_address})는 아직 없음 — 재검색:")
                for dev in devices:
                    _ui.muted(f"     - {dev.name or '(이름없음)'}  {dev.address}")
            else:
                _ui.warn(f"지정 기기({prefer_address})가 안 보입니다. 버튼을 눌러 광고 모드로 전환한 뒤 기다려주세요.")
            continue

        # 2) no specific target: auto-pick only when unambiguous (a single
        #    device). With several advertising, always show the menu so the user
        #    chooses -- even in one-shot mode (matches the app).
        if devices:
            if auto_select and len(devices) == 1:
                dev = devices[0]
                _ui.success(f"기기 1개 자동 선택: [brand]{dev.name or '(이름없음)'}[/] [addr]{dev.address}[/]")
                return dev
            if auto_select:
                _ui.muted("여러 기기가 감지됨 — 선택하세요. ('--address <주소>' 로 다음부터 바로 지정 가능)")
            options = [(_fmt_device_choice(dev), dev) for dev in devices]
            options.append(("↻  다시 검색", "__rescan__"))
            chosen = await _ui.select("제어할 MS605 디바이스를 선택하세요", options)
            if chosen == "__rescan__":
                continue
            return chosen
        _ui.warn("MS605가 안 보입니다. 디바이스 버튼을 눌러 광고(페어링) 모드로 전환한 뒤 기다려주세요.")
        _ui.muted("  (Meross 앱이 연결 중이면 종료/백그라운드 — BLE는 한 번에 하나만 연결됩니다.)")


def _fmt_device_choice(dev: BLEDevice) -> str:
    """One-line label for a scanned device in a selection menu."""
    rssi = getattr(dev, "rssi", None)
    rssi_s = f"{rssi} dBm" if isinstance(rssi, int) else "?"
    return f"{dev.name or '(이름없음)':<22} {dev.address}   RSSI {rssi_s}"


async def connect_with_retry(device: BLEDevice, scan_secs: float, connect_timeout: float) -> MS605:
    """Connect, and on failure keep prompting for the button + retrying
    automatically until the sensor becomes connectable."""
    target = device.address
    first = True
    while True:
        ms = MS605(device)
        try:
            with _ui.status(f"연결 시도: {device.name or ''} {device.address} ..."):
                await ms.connect(timeout=connect_timeout)
            _ui.success("연결 성공!")
            return ms
        except (MS605Error, Exception) as exc:  # noqa: BLE001 - surface any BLE failure
            _ui.error(friendly_ble_error(exc, target))
            if first:
                _ui.warn("디바이스의 버튼을 한 번 눌러 BLE 연결 모드로 전환해주세요.")
                _ui.muted("   버튼을 누르면 자동으로 다시 연결을 시도합니다. (Ctrl-C 로 중단)")
                first = False
            # keep trying: rescan (device may re-advertise a fresh handle after
            # the button press), then reconnect.
            await asyncio.sleep(2.0)
            try:
                found = await MS605.scan(timeout=scan_secs)
            except Exception:  # noqa: BLE001
                found = []
            for d in found:
                if d.address == target:
                    device = d
                    break
            _ui.muted("   ...재시도 중 (버튼 눌러주세요)")


class LiveLink:
    """Keeps a live MS605 GATT link across the interactive session.

    The MS605 drops a link whose central->device direction goes idle, so
    without traffic the connection silently dies while we wait for menu input,
    while the user types values, or while the live monitor only *listens* to
    tag55 pushes (the device stops streaming once it drops us). Two mechanisms
    keep it up:

    * `start_keepalive()` runs a background ping loop for the whole session
      (the app itself pings every ~20 s), so idle waits no longer drop the link
      -- this is what keeps the live monitor's graph updating.
    * `ensure()` still re-establishes the link on demand (rescan + button
      prompt) as a fallback if it did drop, before any device operation.

    The same MS605 object is reused across reconnects, so registered push
    handlers survive and callers can keep their `link.ms` reference."""

    def __init__(
        self,
        ms: MS605,
        device: BLEDevice,
        scan_secs: float,
        connect_timeout: float,
        *,
        keepalive_interval: float = 15.0,
    ) -> None:
        self.ms = ms
        self.device = device
        self.scan_secs = scan_secs
        self.connect_timeout = connect_timeout
        self.keepalive_interval = keepalive_interval
        self._keepalive_task: asyncio.Task | None = None

    def start_keepalive(self) -> None:
        """Begin the background ping loop (idempotent). Keeps the link alive
        through every idle wait until stop_keepalive() / disconnect."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _keepalive_loop(self) -> None:
        # Ping the central->device direction periodically so the MS605 does not
        # drop the idle link. Pings serialise with real operations via the
        # driver's send lock; a failed ping just means the link is already gone
        # (ensure() recovers it on the next operation), so we swallow and retry.
        while True:
            await asyncio.sleep(self.keepalive_interval)
            if not self.ms.is_connected:
                continue
            try:
                await self.ms.ping()
            except Exception:  # noqa: BLE001 - link dropped; ensure() recovers on next op
                pass

    async def ensure(self) -> None:
        """Guarantee a live link before a device operation; reconnect if not."""
        if self.ms.is_connected:
            return
        _ui.warn("BLE 연결이 끊어져 있습니다 (기기가 유휴 상태로 링크를 종료). 재연결합니다...")
        target = self.device.address
        first = True
        while True:
            # rescan first: CoreBluetooth may advertise a fresh handle after the
            # button press, and reusing a stale one just fails again.
            try:
                with _ui.status("재연결용 재검색 중..."):
                    found = await MS605.scan(timeout=self.scan_secs)
            except Exception:  # noqa: BLE001
                found = []
            for d in found:
                if d.address == target:
                    self.device = d
                    break
            try:
                with _ui.status("재연결 시도 중..."):
                    await self.ms.reconnect(device=self.device, timeout=self.connect_timeout)
                _ui.success("재연결 성공!")
                return
            except (MS605Error, Exception) as exc:  # noqa: BLE001 - surface any BLE failure
                _ui.error(friendly_ble_error(exc, target))
                if first:
                    _ui.warn("디바이스 버튼을 한 번 눌러 BLE 연결 모드로 전환해주세요.")
                    _ui.muted("   버튼을 누르면 자동으로 다시 연결합니다. (Ctrl-C 로 중단)")
                    first = False
                await asyncio.sleep(2.0)
                _ui.muted("   ...재시도 중 (버튼 눌러주세요)")
