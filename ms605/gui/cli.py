"""ms605.gui.cli -- the `ms605 gui` subcommand (docs/GUI_API.md section 8).
fastapi/uvicorn/segno are imported inside run_gui() so the other commands do
not pay for them at start-up."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from ipaddress import IPv4Address
from pathlib import Path

LAN_PROBE = ("192.0.2.1", 9)  # TEST-NET-1: connect() on UDP only picks a route, nothing is sent
LOCAL_HOSTS = ("127.0.0.1", "localhost")
GUI_TOKEN_FILE = "gui_access_token"
GUI_SERVER_LOCK_FILE = "gui_server.lock"
_GUI_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class GuiTokenError(Exception):
    pass


class GuiServerLockError(Exception):
    pass


class _GuiServerLock:
    def __init__(self, path: Path, file) -> None:
        self.path = path
        self.file = file

    def close(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def _acquire_gui_server_lock(path: Path) -> _GuiServerLock:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+b")
        os.chmod(path, 0o600)
        if path.stat().st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if "lock_file" in locals():
            lock_file.close()
        raise GuiServerLockError(
            "이미 같은 데이터 디렉터리를 사용하는 서버 Bluetooth GUI가 "
            f"실행 중입니다: {path.parent}"
        ) from exc
    return _GuiServerLock(path, lock_file)


def _read_gui_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise GuiTokenError(f"GUI 접근 토큰 파일을 읽을 수 없습니다: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GuiTokenError(f"GUI 접근 토큰 경로가 일반 파일이 아닙니다: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GuiTokenError(f"GUI 접근 토큰 파일 권한은 0600이어야 합니다: {path}")
        with os.fdopen(fd, encoding="utf-8") as token_file:
            fd = -1
            text = token_file.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise GuiTokenError(f"GUI 접근 토큰 파일을 읽을 수 없습니다: {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    token = text.removesuffix("\n")
    if not _GUI_TOKEN_RE.fullmatch(token):
        raise GuiTokenError(f"GUI 접근 토큰 파일이 손상되었습니다: {path}")
    return token


def _load_or_create_gui_token(path: Path) -> str:
    try:
        return _read_gui_token(path)
    except FileNotFoundError:
        pass

    token = secrets.token_urlsafe(32)
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp_name = tmp.name
            os.chmod(tmp_name, 0o600)
            tmp.write(token + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:  # another GUI created it while this process prepared its candidate
            return _read_gui_token(path)
        return token
    except GuiTokenError:
        raise
    except OSError as exc:
        raise GuiTokenError(f"GUI 접근 토큰 파일을 저장할 수 없습니다: {path}: {exc}") from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


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
        help="HTTPS 웹 GUI 서버 실행 (브라우저·폰에서 접속)",
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
    p.add_argument(
        "--share-port",
        type=_port,
        default=None,
        metavar="PORT",
        help="인증서 없이 접속할 LAN HTTP 공유 포트 (기본: HTTPS 포트 + 1, 0 = 빈 포트, --lan과 함께)",
    )
    p.add_argument(
        "--ssl-certfile", default=None, metavar="PATH", help="자동 생성 인증서 대신 사용할 HTTPS 인증서"
    )
    p.add_argument("--ssl-keyfile", default=None, metavar="PATH", help="HTTPS 인증서의 개인 키 (--ssl-certfile과 함께)")
    p.add_argument("--sim", type=_sim_count, default=None, metavar="N", help="실기기 대신 가상 센서 N대(1~32)")
    p.add_argument(
        "--ble-transport",
        choices=("browser", "server"),
        default="browser",
        help="BLE 연결 위치 (기본 browser: 접속 브라우저, server: GUI 서버 컴퓨터)",
    )
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
        if getattr(namespace, "share_port", None) is not None and not getattr(namespace, "lan", False):
            p.error("--share-port는 --lan과 함께만 쓸 수 있습니다")
        if bool(namespace.ssl_certfile) != bool(namespace.ssl_keyfile):
            p.error("--ssl-certfile과 --ssl-keyfile은 함께 써야 합니다")
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


def _listener(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


async def _serve_all(servers_and_sockets: list[tuple[object, socket.socket]]) -> None:
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]))
        for server, sock in servers_and_sockets
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server, _sock in servers_and_sockets:
            server.should_exit = True
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def run_gui(args: argparse.Namespace, *, scan_secs: float, connect_timeout: float) -> int:
    import segno
    import uvicorn

    from ms605.errors import StorageError
    from ms605.fleet import Fleet
    from ms605.registry import Registry
    from ms605.session import KEEPALIVE_INTERVAL_S
    from ms605.sim import SimFleet
    from ms605.storage import Storage, data_root

    from .browser_ble import BrowserBluetooth
    from .server import create_app
    from .tls import TLSError, ensure_gui_tls

    lan, port = bool(args.lan), args.port
    requested_port = port
    sim_count = getattr(args, "sim", None)
    ble_transport = getattr(args, "ble_transport", "browser")
    speed = getattr(args, "speed", None) or 1.0
    certfile, keyfile = getattr(args, "ssl_certfile", None), getattr(args, "ssl_keyfile", None)
    scheme = "https"

    storage = Storage(root=data_root() / "cal_results" / "sim") if sim_count else Storage()
    try:
        registry = Registry(storage)
    except StorageError as exc:
        print(f"레지스트리 파일 오류: {exc}", file=sys.stderr)
        return 2
    sim = None
    browser_ble = None
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
    elif ble_transport == "browser":
        browser_ble = BrowserBluetooth()
        fleet = Fleet(
            registry, storage, scan=browser_ble.scan, client_factory=browser_ble.client_factory,
            scan_secs=scan_secs, connect_timeout=connect_timeout,
        )
    else:
        fleet = Fleet(
            registry, storage, scan_secs=scan_secs, connect_timeout=connect_timeout,
        )

    bind_host = "0.0.0.0" if lan else "127.0.0.1"
    try:
        sock = _listener(bind_host, port)
    except OSError as exc:
        print(f"포트 {port}을(를) 열 수 없습니다: {exc}. --port로 다른 포트를 지정하세요.", file=sys.stderr)
        return 2
    port = sock.getsockname()[1]
    share_sock = None
    share_port = None
    if lan:
        requested_share_port = getattr(args, "share_port", None)
        if requested_share_port is None:
            requested_share_port = port + 1 if requested_port != 0 and port < 65535 else 0
        try:
            share_sock = _listener(bind_host, requested_share_port)
        except OSError as exc:
            sock.close()
            print(
                f"공유 포트 {requested_share_port}을(를) 열 수 없습니다: {exc}. "
                "--share-port로 다른 포트를 지정하세요.",
                file=sys.stderr,
            )
            return 2
        share_port = share_sock.getsockname()[1]

    auto_ip = lan_ip() if lan else None
    tailnet = tailnet_hosts() if lan else []
    lan_host = getattr(args, "lan_host", None)
    ip = lan_host.lower() if lan_host else auto_ip  # browsers send a lowercase Host
    hosts = allowed_hosts(lan, ip, auto_ip, *tailnet)
    ca_certfile = None
    if certfile is None:
        try:
            tls = ensure_gui_tls(data_root().resolve(), hosts)
        except TLSError as exc:
            sock.close()
            if share_sock is not None:
                share_sock.close()
            print(str(exc), file=sys.stderr)
            return 2
        certfile, keyfile, ca_certfile = str(tls.certfile), str(tls.keyfile), tls.ca_certfile
    try:
        token = _load_or_create_gui_token(storage.root / GUI_TOKEN_FILE)
    except GuiTokenError as exc:
        sock.close()
        if share_sock is not None:
            share_sock.close()
        await fleet.aclose()
        print(str(exc), file=sys.stderr)
        return 2
    app = create_app(
        fleet, registry, storage, token, allowed_hosts=hosts,
        sim=sim, lan=lan, browser_ble=browser_ble, ble_transport=ble_transport,
    )

    server_lock = None
    if ble_transport == "server":
        try:
            server_lock = _acquire_gui_server_lock(storage.root / GUI_SERVER_LOCK_FILE)
        except GuiServerLockError as exc:
            sock.close()
            if share_sock is not None:
                share_sock.close()
            await fleet.aclose()
            print(str(exc), file=sys.stderr)
            return 2

    print(f"ms605 gui: {scheme}://127.0.0.1:{port}/?t={token}", flush=True)
    if ca_certfile is not None:
        print(f"HTTPS 신뢰 인증서: {ca_certfile}", flush=True)
        print(
            "최초 한 번, 접속할 각 기기에 이 CA 인증서를 신뢰 등록하세요. 개인 키 파일은 공유하지 마세요.",
            flush=True,
        )
    if sim is not None:
        print(f"시뮬레이터: 센서 {sim_count}대, 속도 x{speed:g}", flush=True)
    elif ble_transport == "browser":
        print("센서는 접속한 브라우저 기기의 Bluetooth로 연결합니다. Chrome/Edge에서 센서를 선택하세요.", flush=True)
    else:
        print("센서는 GUI 서버 컴퓨터의 Bluetooth로 연결합니다. 서버 근처에서 센서 버튼을 누르세요.", flush=True)
    if lan:
        share_host = ip or f"{host_name()}.local"
        https_url = f"{scheme}://{share_host}:{port}/?t={token}"
        http_url = f"http://{share_host}:{share_port}/?t={token}"
        if ip is None:
            print("LAN 주소를 찾지 못했습니다. 같은 네트워크에서 아래 .local 주소로 접속해 보세요.", flush=True)
        elif lan_host is None:
            print("폰에서 열리지 않으면(VPN 등) --lan-host <이 컴퓨터의 Wi-Fi 주소>로 다시 실행하세요.", flush=True)
        print(f"로컬 HTTP 주소: http://127.0.0.1:{share_port}/?t={token}", flush=True)
        if ble_transport == "browser":
            share_url = https_url
            print(f"LAN 공유 주소 (브라우저 Bluetooth): {share_url}", flush=True)
            print(f"LAN HTTP 보기 주소: {http_url}", flush=True)
            print(
                "브라우저 Bluetooth에는 HTTPS가 필요합니다. QR은 HTTPS 공유 주소를 사용합니다.",
                flush=True,
            )
        else:
            share_url = http_url
            print(f"LAN HTTPS 주소: {https_url}", flush=True)
            print(f"LAN 공유 주소 (서버 Bluetooth): {share_url}", flush=True)
        segno.make(share_url, error="m").terminal(out=sys.stdout, compact=True)
        for host in tailnet:
            print(f"Tailnet 주소: {scheme}://{host}:{port}/?t={token}", flush=True)
        if not tailnet:
            print("Tailnet 주소를 찾지 못했습니다. Tailscale 연결 상태를 확인하세요.", flush=True)
        print("주의: 이 주소를 가진 사람은 누구나 센서를 조작할 수 있습니다. 공유하지 마세요.", flush=True)
    print("종료하려면 Ctrl-C를 누르세요.", flush=True)

    config = uvicorn.Config(
        app, lifespan="on", log_level="warning", access_log=False, timeout_graceful_shutdown=5,
        ssl_certfile=certfile, ssl_keyfile=keyfile,
    )
    server = uvicorn.Server(config)
    servers_and_sockets = [(server, sock)]
    if share_sock is not None:
        share_config = uvicorn.Config(
            app, lifespan="off", log_level="warning", access_log=False, timeout_graceful_shutdown=5,
        )
        servers_and_sockets.append((uvicorn.Server(share_config), share_sock))
    try:
        await _serve_all(servers_and_sockets)
    finally:
        try:
            await fleet.aclose()  # the lifespan already did this unless start-up failed
        finally:
            for _server, listener in servers_and_sockets:
                listener.close()
            if server_lock is not None:
                server_lock.close()
    return 0
