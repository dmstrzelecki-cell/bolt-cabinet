"""The real Fetcher over a real TLS socket: pins the vetted IP, sends SNI for the
hostname, and verifies the certificate against the hostname (not the IP)."""
from __future__ import annotations

import datetime
import http.server
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import safefetch
from safefetch import Fetcher, FetchError


def make_cert(tmp_path, name):
    key = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return cp, kp


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    cp, kp = make_cert(tmp_path, "approved.example.org")
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers["Host"]
            seen["ua"] = self.headers["User-Agent"]
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "https://other.example.org/")
                self.end_headers()
                return
            body = b"<html><title>hi</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cp, kp)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # Our fetcher always dials port 443; point that at the test server.
    real = safefetch.socket.create_connection
    monkeypatch.setattr(safefetch.socket, "create_connection",
                        lambda addr, timeout=None: real((addr[0], port), timeout))
    cctx = ssl.create_default_context(cafile=str(cp))
    yield cctx, seen
    srv.shutdown()


def test_pinned_tls_fetch_and_cert_check(tls_server):
    ctx, seen = tls_server
    f = Fetcher(resolve=lambda h: ["127.0.0.1"], ip_ok=lambda ip: True, context=ctx)
    r = f.get("https://approved.example.org/", {"approved.example.org"})
    assert r.status == 200 and b"<title>hi" in r.body
    assert seen["host"] == "approved.example.org" and seen["ua"].startswith("StrzFamilySearch/1.0")
    # A hostname the certificate does not cover fails verification.
    with pytest.raises(FetchError) as e:
        f.get("https://wrong.example.org/", {"wrong.example.org"})
    assert e.value.kind == "tls"
    # Redirect to an unapproved host is refused before connecting to it.
    with pytest.raises(FetchError) as e:
        f.get("https://approved.example.org/redir", {"approved.example.org"})
    assert e.value.kind == "unapproved"


def test_default_policy_refuses_loopback_before_connecting(tls_server):
    ctx, seen = tls_server
    f = Fetcher(resolve=lambda h: ["127.0.0.1"], context=ctx)  # real ip_allowed
    with pytest.raises(FetchError) as e:
        f.get("https://approved.example.org/", {"approved.example.org"})
    assert e.value.kind == "blocked_ip" and "host" not in seen
