"""Make-only GUI launcher: gracefully replace the GUI on the selected port."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _listeners(port: int) -> list[int]:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, timeout=2,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"포트 {port}의 기존 MS605 GUI를 확인할 수 없습니다")
    return sorted({int(pid) for pid in result.stdout.split()})


def _running(pids: list[int]) -> bool:
    result = subprocess.run(
        ["ps", "-p", ",".join(map(str, pids)), "-o", "stat="],
        capture_output=True, text=True, timeout=2,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("기존 MS605 GUI의 종료 상태를 확인할 수 없습니다")
    return any(not state.startswith("Z") for state in result.stdout.split())


def _is_gui(command: str) -> bool:
    args = shlex.split(command)
    if len(args) < 2:
        return False
    executable = shutil.which(args[0])
    if not executable:
        return False
    entrypoint = (ROOT / ".venv/bin/ms605").resolve()
    if Path(executable).resolve() == entrypoint:
        return args[1] == "gui"
    if Path(executable).resolve() != Path(sys.executable).resolve():
        return False
    return (
        Path(args[1]).is_absolute() and Path(args[1]).resolve() == entrypoint and args[2:3] == ["gui"]
    ) or args[1:4] == ["-m", "ms605.cli.cli", "gui"]


def _stop_existing(port: int) -> None:
    if port == 0:
        return
    pids = _listeners(port)
    if not pids:
        return
    if not hasattr(os, "getuid"):
        raise RuntimeError("이 플랫폼에서는 기존 MS605 GUI를 자동으로 확인할 수 없습니다")
    # Validate every listener before sending any signal.
    for pid in pids:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=", "-o", "args="],
            capture_output=True, text=True, timeout=2,
        )
        details = process.stdout.strip().split(maxsplit=1)
        if (
            process.returncode != 0
            or len(details) != 2
            or details[0] != str(os.getuid())
            or pid == os.getpid()
            or not _is_gui(details[1])
        ):
            raise RuntimeError(f"포트 {port}의 프로세스 {pid}는 현재 사용자의 MS605 GUI가 아니므로 종료하지 않습니다")
    print(f"기존 MS605 GUI 종료 중 (포트 {port}, PID {', '.join(map(str, pids))})", flush=True)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _running(pids) and not _listeners(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"기존 MS605 GUI가 10초 안에 포트 {port}를 해제하지 않았습니다")


def main() -> int:
    from ms605.cli.cli import build_parser

    os.chdir(ROOT)
    argv = sys.argv[1:]
    # Help and invalid options must not stop a running server.
    args = build_parser().parse_args(["gui", "--lan", *argv])
    try:
        _stop_existing(args.port)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"MS605 GUI 재시작 실패: {exc}", file=sys.stderr)
        return 2
    launcher = str(ROOT / "scripts/run.sh")
    os.execv(launcher, [launcher, "gui", "--lan", *argv])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
