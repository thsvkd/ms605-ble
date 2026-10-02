"""ms605.gui.cli -- the `ms605 gui` subcommand (docs/GUI_API.md section 8).
fastapi/uvicorn/segno are imported inside run_gui() so the other commands do
not pay for them at start-up."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from ipaddress import IPv4Address
from pathlib import Path

LAN_PROBE = ("192.0.2.1", 9)  # TEST-NET-1: connect() on UDP only picks a route, nothing is sent
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _port(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0..65535, got {value}")
    return value


def _sim_count(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 32:
        raise argparse.ArgumentTypeError(f"--sim must be 1..32, got {value}")
    return value


def _speed(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"--speed must be > 0, got {text}")
    return value


def add_parser(sub: argparse._SubParsersAction) -> None:
    from ms605.cli.cli import _add_target_args  # called from build_parser(): the module is loaded by then

    p = sub.add_parser(
        "gui",
        help="웹 GUI 서버 실행 (브라우저·폰에서 접속)",
        description="센서 대시보드와 센서 모으기 화면을 웹으로 엽니다. 출력된 주소(토큰 포함)를 브라우저에서 여세요.",
    )
    p.add_argument("--lan", action="store_true", help="LAN·연결된 tailnet 접속 허용, 주소와 LAN QR 출력")
    p.add_argument(
        "--lan-host",
        default=None,
        metavar="ADDR",
        help="LAN 주소·QR에 쓸 이 컴퓨터의 주소 (기본: 자동. VPN 등으로 폰에서 안 열리면 Wi-Fi 주소, --lan과 함께)",
    )
    p.add_argument("--port", type=_port, default=8605, help="포트 (기본 8605, 0 = 빈 포트)")
    p.add_argument("--sim", type=_sim_count, default=None, metavar="N", help="실기기 대신 가상 센서 N대(1~32)")
    p.add_argument(
        "--speed", type=_speed, default=None, metavar="K", help="시뮬레이터 시간 배속 (기본 1, --sim과 함께)"
    )
    _add_target_args(p, suppress_defaults=True)

    parse_known_args = p.parse_known_args

    def parse_and_check(args=None, namespace=None):
        namespace, rest = parse_known_args(args, namespace)
        if getattr(namespace, "speed", None) is not None and getattr(namespace, "sim", None) is None:
            p.error("--speed는 --sim과 함께만 쓸 수 있습니다")
        if getattr(namespace, "lan_host", None) is not None and not getattr(namespace, "lan", False):
            p.error("--lan-host는 --lan과 함께만 쓸 수 있습니다")
        return namespace, rest

    p.parse_known_args = parse_and_check  # the subparsers action calls this on the gui parser


def lan_ip() -> str | None:
    """This host's address on the default route, or None (none found, or loopback)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(LAN_PROBE)
            ip = probe.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def host_name() -> str:
    """socket.gethostname() lowercased (browsers send a lowercase Host) without a trailing ".local"."""
    return socket.gethostname().lower().removesuffix(".local")


def tailnet_hosts() -> list[str]:
    """Discover this node's reachable IPv4 hosts without changing Tailscale state."""
    command = shutil.which("tailscale")
    if command is None and sys.platform == "darwin":
        app = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        if app.is_file():
            command = str(app)
    if command is None:
        return []
    try:
        result = subprocess.run(
            [command, "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "TAILSCALE_BE_CLI": "1"},
        )
        if result.returncode != 0:
            return []
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return []
    addresses = status.get("TailscaleIPs", [])
    if not isinstance(addresses, list):
        return []
    hosts = []
    for address in addresses:
        if not isinstance(address, str):
            continue
        try:
            host = str(IPv4Address(address))
        except ValueError:
            continue  # the listener is IPv4; do not advertise IPv6-only endpoints
        if host not in hosts:
            hosts.append(host)
    node = status.get("Self")
    tailnet = status.get("CurrentTailnet")
    if hosts and isinstance(node, dict) and isinstance(tailnet, dict) and tailnet.get("MagicDNSEnabled") is True:
        name = node.get("DNSName")
        if isinstance(name, str) and name.rstrip("."):
            hosts.append(name.lower().rstrip("."))
    return hosts


def allowed_hosts(lan: bool, *ips: str | None) -> list[str]:
    hosts = list(LOCAL_HOSTS)
    if lan:
        name = host_name()
        for host in (*ips, name, f"{name}.local"):
            if host and host not in hosts:
                hosts.append(host)
    return hosts


async def run_gui(args: argparse.Namespace, *, scan_secs: float, connect_timeout: float) -> int:
    import segno
    import uvicorn

    from ms605.errors import StorageError
    from ms605.fleet import Fleet
    from ms605.registry import Registry
    from ms605.session import KEEPALIVE_INTERVAL_S
    from ms605.sim import SimFleet
    from ms605.storage import Storage, data_root

    from .server import create_app

    lan, port = bool(args.lan), args.port
    sim_count = getattr(args, "sim", None)
    speed = getattr(args, "speed", None) or 1.0

    token = secrets.token_urlsafe(32)
    storage = Storage(root=data_root() / "cal_results" / "sim") if sim_count else Storage()
    try:
        registry = Registry(storage)
    except StorageError as exc:
        print(f"레지스트리 파일 오류: {exc}", file=sys.stderr)
        return 2
    sim = None
    if sim_count:
        sim = SimFleet(sim_count, speed=speed)
        fleet = Fleet(
            registry,
            storage,
            scan=sim.discover,
            client_factory=sim.client_factory,
            keepalive_interval=KEEPALIVE_INTERVAL_S / speed,  # the 30 s idle drop shrinks by the same factor
            scan_secs=scan_secs,
            connect_timeout=connect_timeout,
        )
    else:
        fleet = Fleet(registry, storage, scan_secs=scan_secs, connect_timeout=connect_timeout)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0" if lan else "127.0.0.1", port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        print(f"포트 {port}을(를) 열 수 없습니다: {exc}. --port로 다른 포트를 지정하세요.", file=sys.stderr)
        return 2
    port = sock.getsockname()[1]

    auto_ip = lan_ip() if lan else None
    tailnet = tailnet_hosts() if lan else []
    lan_host = getattr(args, "lan_host", None)
    ip = lan_host.lower() if lan_host else auto_ip  # browsers send a lowercase Host
    app = create_app(
        fleet, registry, storage, token, allowed_hosts=allowed_hosts(lan, ip, auto_ip, *tailnet), sim=sim, lan=lan
    )

    print(f"ms605 gui: http://127.0.0.1:{port}/?t={token}", flush=True)
    if sim is not None:
        print(f"시뮬레이터: 센서 {sim_count}대, 속도 x{speed:g}", flush=True)
    if lan:
        if ip is None:
            url = f"http://{host_name()}.local:{port}/?t={token}"  # an IP would not pass the Host check
            print(f"LAN 주소를 찾지 못했습니다. 같은 네트워크의 기기에서 이 주소로 접속해 보세요: {url}", flush=True)
        else:
            url = f"http://{ip}:{port}/?t={token}"
            print(f"LAN 주소: {url}", flush=True)
            segno.make(url, error="m").terminal(out=sys.stdout, compact=True)
            if lan_host is None:
                print("폰에서 열리지 않으면(VPN 등) --lan-host <이 컴퓨터의 Wi-Fi 주소>로 다시 실행하세요.", flush=True)
        for host in tailnet:
            print(f"Tailnet 주소: http://{host}:{port}/?t={token}", flush=True)
        if not tailnet:
            print("Tailnet 주소를 찾지 못했습니다. Tailscale 연결 상태를 확인하세요.", flush=True)
        print("주의: 이 주소를 가진 사람은 누구나 센서를 조작할 수 있습니다. 공유하지 마세요.", flush=True)
    print("종료하려면 Ctrl-C를 누르세요.", flush=True)

    config = uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False, timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    try:
        await server.serve(sockets=[sock])
    finally:
        await fleet.aclose()  # the lifespan already did this unless start-up failed
        sock.close()
    return 0
