"""Create and reuse a local CA and hostname-verified HTTPS certificate for the GUI."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path


class TLSError(RuntimeError):
    """The GUI's HTTPS certificate could not be prepared."""


@dataclass(frozen=True)
class TLSFiles:
    certfile: Path
    keyfile: Path
    ca_certfile: Path


def _run(command: str, *args: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [command, *(str(arg) for arg in args)], capture_output=True, text=True, timeout=15,
    )
    if check and result.returncode:
        raise TLSError(f"OpenSSL 인증서 준비 실패: {result.stderr.strip()}")
    return result


def _usable(command: str, cert: Path, key: Path) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key, password="")
    except (OSError, ValueError):
        return False
    return _run(command, "x509", "-in", cert, "-noout", "-checkend", "604800", check=False).returncode == 0


def _covers(files: TLSFiles, hosts: Sequence[str]) -> bool:
    """Validate trust and SANs without a listener or version-specific OpenSSL flags."""
    try:
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(files.certfile, files.keyfile, password="")
        client_context = ssl.create_default_context(cafile=files.ca_certfile)
        for host in hosts:
            client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
            client = client_context.wrap_bio(client_in, client_out, server_hostname=host)
            server = server_context.wrap_bio(server_in, server_out, server_side=True)
            for _ in range(20):
                try:
                    client.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pass
                server_in.write(client_out.read())
                try:
                    server.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                client_in.write(server_out.read())
            else:
                return False
        return True
    except (OSError, ValueError):
        return False


def _host_entries(hosts: Sequence[str]) -> list[tuple[str, str]]:
    entries = []
    for host in dict.fromkeys(hosts):
        try:
            entries.append(("IP", str(ip_address(host))))
        except ValueError:
            name = host.encode("idna").decode("ascii").lower()
            if len(name) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")
            ):
                raise TLSError(f"HTTPS 인증서에 사용할 수 없는 호스트 이름: {host!r}") from None
            entries.append(("DNS", name))
    return entries


def ensure_gui_tls(root: Path, hosts: Sequence[str]) -> TLSFiles:
    """Keep the CA stable; renew the server certificate when expired or hosts change.

    Trust installation belongs to each client device. Only ca.pem is public;
    both private keys stay in the application's private data directory.
    """
    command = shutil.which("openssl")
    if command is None:
        raise TLSError(
            "자동 HTTPS 인증서 생성에는 OpenSSL이 필요합니다. 설치하거나 --ssl-certfile/--ssl-keyfile을 지정하세요."
        )
    try:
        entries = _host_entries(hosts)
        directory = root / "cal_results" / "gui_tls"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        files = TLSFiles(directory / "cert.pem", directory / "key.pem", directory / "ca.pem")
        ca_key = directory / "ca-key.pem"
        ca_valid = _usable(command, files.ca_certfile, ca_key)
        if ca_valid and _usable(command, files.certfile, files.keyfile) and _covers(files, [h for _, h in entries]):
            ca_key.chmod(0o600)
            files.keyfile.chmod(0o600)
            return files

        with tempfile.TemporaryDirectory(prefix=".generate-", dir=directory) as temporary:
            staging = Path(temporary)
            config = staging / "openssl.cnf"
            config_text = (
                "[req]\nprompt = no\ndistinguished_name = dn\n[dn]\nCN = {name}\n"
                "[ca]\nbasicConstraints = critical, CA:TRUE, pathlen:0\n"
                "keyUsage = critical, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n"
                "[server]\nbasicConstraints = critical, CA:FALSE\n"
                "keyUsage = critical, digitalSignature, keyEncipherment\n"
                "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid, issuer\n"
                "extendedKeyUsage = serverAuth\nsubjectAltName = @san\n[san]\n"
                + "".join(f"{kind}.{index} = {host}\n" for index, (kind, host) in enumerate(entries, 1))
            )
            config.write_text(config_text.format(name="MS605 GUI"), encoding="utf-8")
            signing_cert, signing_key = files.ca_certfile, ca_key
            if not ca_valid:
                signing_cert, signing_key = staging / "ca.pem", staging / "ca-key.pem"
                ca_config = staging / "ca.cnf"
                ca_config.write_text(config_text.format(name="MS605 Local CA"), encoding="utf-8")
                _run(
                    command, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                    "-days", "3650", "-config", ca_config, "-extensions", "ca",
                    "-out", signing_cert, "-keyout", signing_key,
                )
            _run(
                command, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
                "-config", config, "-out", staging / "server.csr", "-keyout", staging / "key.pem",
            )
            _run(
                command, "x509", "-req", "-in", staging / "server.csr", "-CA", signing_cert,
                "-CAkey", signing_key, "-set_serial", f"0x{secrets.token_hex(16)}", "-days", "90",
                "-sha256", "-extfile", config, "-extensions", "server", "-out", staging / "cert.pem",
            )
            prepared = TLSFiles(staging / "cert.pem", staging / "key.pem", signing_cert)
            if not _covers(prepared, [h for _, h in entries]):
                raise TLSError("생성한 HTTPS 인증서의 CA 신뢰 또는 호스트 검증에 실패했습니다.")
            names = ["cert.pem", "key.pem"]
            if not ca_valid:
                names.extend(["ca.pem", "ca-key.pem"])
            for name in names:
                (staging / name).chmod(0o600 if "key" in name else 0o644)
                os.replace(staging / name, directory / name)
        return files
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TLSError(f"HTTPS 인증서를 준비할 수 없습니다: {exc}") from exc
