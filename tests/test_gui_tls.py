"""Generated GUI certificates must work with normal TLS trust and hostname checks."""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import pytest

from ms605.gui import tls
from ms605.gui.tls import TLSError, ensure_gui_tls


def _handshake(files, hostname: str) -> None:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(files.certfile, files.keyfile)
    client_context = ssl.create_default_context(cafile=files.ca_certfile)
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(20):
        try:
            client.do_handshake()
            return
        except ssl.SSLWantReadError:
            pass
        server_in.write(client_out.read())
        try:
            server.do_handshake()
        except ssl.SSLWantReadError:
            pass
        client_in.write(server_out.read())
    raise AssertionError("TLS handshake did not complete")


def test_automatic_certificates_cover_gui_hosts_and_are_reused(tmp_path):
    hosts = ["127.0.0.1", "localhost", "192.0.2.10", "testhost.local", "node.test.ts.net"]
    files = ensure_gui_tls(tmp_path, hosts)
    assert files.ca_certfile == tmp_path / "cal_results" / "gui_tls" / "ca.pem"
    assert files.certfile.parent == files.ca_certfile.parent == files.keyfile.parent
    for host in hosts:
        _handshake(files, host)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files.certfile.parent.iterdir()}
    assert ensure_gui_tls(tmp_path, hosts) == files
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before
    assert files.keyfile.stat().st_mode & 0o777 == 0o600
    assert (files.keyfile.parent / "ca-key.pem").stat().st_mode & 0o777 == 0o600


def test_new_hosts_refresh_only_the_server_certificate(tmp_path):
    files = ensure_gui_tls(tmp_path, ["localhost"])
    ca_before = files.ca_certfile.read_bytes()
    cert_before = files.certfile.read_bytes()
    files = ensure_gui_tls(tmp_path, ["localhost", "192.0.2.20"])
    assert files.ca_certfile.read_bytes() == ca_before
    assert files.certfile.read_bytes() != cert_before
    _handshake(files, "192.0.2.20")
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(files, "other.example")


def test_damaged_server_key_is_repaired_without_changing_ca(tmp_path):
    files = ensure_gui_tls(tmp_path, ["localhost"])
    ca_before = files.ca_certfile.read_bytes()
    files.keyfile.write_text("damaged key")
    ensure_gui_tls(tmp_path, ["localhost"])
    assert files.ca_certfile.read_bytes() == ca_before
    _handshake(files, "localhost")


def test_openssl_missing_has_actionable_error_and_does_not_create_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tls.shutil, "which", lambda _: None)
    with pytest.raises(TLSError, match="OpenSSL.*--ssl-certfile/--ssl-keyfile"):
        ensure_gui_tls(tmp_path, ["localhost"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host", ["host\nDNS.2=evil.example", "*.example", "bad/host", chr(0xD800)])
def test_invalid_hosts_fail_before_writing_certificates(tmp_path, host):
    with pytest.raises(TLSError):
        ensure_gui_tls(tmp_path, [host])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/openssl").is_file(), reason="stock macOS LibreSSL")
def test_stock_macos_openssl_certificates_are_trusted_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(tls.shutil, "which", lambda _: "/usr/bin/openssl")
    test_automatic_certificates_cover_gui_hosts_and_are_reused(tmp_path)
