"""ms605.cli._shared -- helpers shared by the MS605 CLIs.

`ainput`/`_prompt` provide non-blocking interactive input for cli.py.
`discover_and_select`/`connect_with_retry`/`LiveLink` implement the
single-device, button-press-aware connect flow used by the interactive app and
one-shot subcommands (and by tools/debug_zone_write.py) -- not by the
`ms605 calibrate` batch path, which manages several devices at once with its
own pooling logic (see run_batch_calibration in cli.py).
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from ms605 import MS605, MS605Error
from ms605.discovery import friendly_ble_error

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice


async def ainput(prompt: str = "") -> str:
    """Non-blocking input() so the event loop (notify callbacks) keeps running."""
    loop = asyncio.get_running_loop()
    return (
        (await loop.run_in_executor(None, sys.stdin.readline)).rstrip("\n")
        if not prompt
        else (await loop.run_in_executor(None, _prompt, prompt))
    )


def _prompt(text: str) -> str:
    try:
        return input(text)
    except EOFError:
        return ""


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
        print(f"\n🔍 MS605 검색 중... (스캔 {scan_secs:.0f}s, 시도 {attempt})")
        devices = await MS605.scan(timeout=scan_secs)

        # 1) a specific device was requested: take it the instant it appears,
        #    no matter how many others are around; otherwise keep scanning.
        if want is not None:
            for dev in devices:
                if _is_target(dev):
                    print(f"\n➡️  지정 기기 자동 선택: {dev.name or '(이름없음)'} {dev.address}")
                    return dev
            if devices:
                print(f"\n발견된 MS605 {len(devices)}개 중 지정 기기({prefer_address})는 아직 없음 — 재검색:")
                for dev in devices:
                    print(f"     - {dev.name or '(이름없음)':<20} {dev.address}")
            else:
                print(f"  → 지정 기기({prefer_address})가 안 보입니다. 버튼을 눌러 광고 모드로 전환한 뒤 기다려주세요.")
            continue

        # 2) no specific target: auto-pick only when unambiguous (a single
        #    device). With several advertising, always show the numbered menu so
        #    the user chooses -- even in one-shot mode (matches the app).
        if devices:
            print(f"\n발견된 MS605 디바이스 {len(devices)}개:")
            for idx, dev in enumerate(devices):
                rssi = getattr(dev, "rssi", None)
                rssi_s = f"{rssi} dBm" if isinstance(rssi, int) else "?"
                print(f"  [{idx}] {dev.name or '(이름없음)':<20} {dev.address}   RSSI {rssi_s}")
            if auto_select and len(devices) == 1:
                dev = devices[0]
                print(f"\n➡️  기기 1개 자동 선택: [0] {dev.name or '(이름없음)'} {dev.address}")
                return dev
            if auto_select:
                print(
                    "  (여러 기기가 감지됨 — 번호를 선택하세요. "
                    "'--address <주소>' 로 다음부터 바로 지정할 수 있습니다.)"
                )
            print("  [r] 다시 검색")
            choice = (await ainput("\n제어할 디바이스 번호 선택: ")).strip().lower()
            if choice == "r":
                continue
            if choice.isdigit() and 0 <= int(choice) < len(devices):
                return devices[int(choice)]
            print("잘못된 입력입니다.")
            continue
        print("  → MS605가 안 보입니다. 디바이스 버튼을 눌러 광고(페어링) 모드로 전환한 뒤 기다려주세요.")
        print("    (Meross 앱이 연결 중이면 종료/백그라운드 처리 — BLE는 한 번에 하나만 연결됩니다.)")


async def connect_with_retry(device: BLEDevice, scan_secs: float, connect_timeout: float) -> MS605:
    """Connect, and on failure keep prompting for the button + retrying
    automatically until the sensor becomes connectable."""
    target = device.address
    first = True
    while True:
        ms = MS605(device)
        try:
            print(f"\n🔗 연결 시도: {device.name or ''} {device.address} ...")
            await ms.connect(timeout=connect_timeout)
            print("✅ 연결 성공!")
            return ms
        except (MS605Error, Exception) as exc:  # noqa: BLE001 - surface any BLE failure
            print(f"   ✗ {friendly_ble_error(exc, target)}")
            if first:
                print("\n👉 디바이스의 버튼을 한 번 눌러 BLE 연결 모드로 전환해주세요.")
                print("   버튼을 누르면 자동으로 다시 연결을 시도합니다. (Ctrl-C 로 중단)")
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
            print("   ...재시도 중 (버튼 눌러주세요)")


class LiveLink:
    """Keeps a live MS605 GATT link across the interactive session.

    The MS605 drops idle BLE links, so between menu selections (or while the
    user is typing values) the connection can silently go away -- bleak then
    reports it as "Service Discovery has not been performed yet". `ensure()`
    re-establishes the link on demand (rescanning for a fresh handle and
    prompting for the button) so every device operation runs on a live link.
    The same MS605 object is reused across reconnects, so registered push
    handlers survive and callers can keep their `link.ms` reference."""

    def __init__(
        self,
        ms: MS605,
        device: BLEDevice,
        scan_secs: float,
        connect_timeout: float,
    ) -> None:
        self.ms = ms
        self.device = device
        self.scan_secs = scan_secs
        self.connect_timeout = connect_timeout

    async def ensure(self) -> None:
        """Guarantee a live link before a device operation; reconnect if not."""
        if self.ms.is_connected:
            return
        print("\n⚠️  BLE 연결이 끊어져 있습니다 (기기가 유휴 상태로 링크를 종료). 재연결합니다...")
        target = self.device.address
        first = True
        while True:
            # rescan first: CoreBluetooth may advertise a fresh handle after the
            # button press, and reusing a stale one just fails again.
            try:
                found = await MS605.scan(timeout=self.scan_secs)
            except Exception:  # noqa: BLE001
                found = []
            for d in found:
                if d.address == target:
                    self.device = d
                    break
            try:
                await self.ms.reconnect(device=self.device, timeout=self.connect_timeout)
                print("✅ 재연결 성공!\n")
                return
            except (MS605Error, Exception) as exc:  # noqa: BLE001 - surface any BLE failure
                print(f"   ✗ {friendly_ble_error(exc, target)}")
                if first:
                    print("\n👉 디바이스 버튼을 한 번 눌러 BLE 연결 모드로 전환해주세요.")
                    print("   버튼을 누르면 자동으로 다시 연결합니다. (Ctrl-C 로 중단)")
                    first = False
                await asyncio.sleep(2.0)
                print("   ...재시도 중 (버튼 눌러주세요)")
