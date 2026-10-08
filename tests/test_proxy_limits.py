"""Exercise TLS through the guard and real rate-limit refill intervals."""

import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from api.main import create_app
from api.ratelimit import RateLimiter
from api.settings import Settings
from api.ssrf_guard import SSRFGuard


@pytest.fixture
def tls_endpoint(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, ssl.create_default_context(cafile=str(cert_file)), received
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_https_connect_preserves_tls_verification(tls_endpoint):
    port, context, received = tls_endpoint
    guard = SSRFGuard(allow_local=True).start()
    try:
        with httpx.Client(proxy=guard.proxy_url, verify=context, timeout=5) as client:
            response = client.post(
                f"https://localhost:{port}/v1/chat/completions", json={"model": "test"}
            )
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "ok"
            # The same server's certificate must not validate for a different hostname.
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                client.post(f"https://127.0.0.1:{port}/v1/chat/completions", json={})
        assert len(received) == 1
    finally:
        guard.stop()


def test_https_connect_blocks_private_dns_at_dial(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "rebind.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
            ]
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    guard = SSRFGuard().start()
    try:
        with (
            httpx.Client(proxy=guard.proxy_url, timeout=5) as client,
            pytest.raises(httpx.ProxyError, match="403"),
        ):
            client.get("https://rebind.example/")
    finally:
        guard.stop()


def test_account_refill_uses_minutes(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("api.ratelimit.time.monotonic", lambda: clock[0])
    limiter = RateLimiter(rpm=30, burst=1, signup_per_min=5, signup_burst=1)
    assert limiter.take_account(1) == 0
    assert limiter.take_account(1) == pytest.approx(2)
    clock[0] = 1
    assert limiter.take_account(1) == pytest.approx(1)
    clock[0] = 2
    assert limiter.take_account(1) == 0


def test_spoofed_forwarded_header_cannot_reset_signup_bucket(tmp_path):
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/limits.db",
            enable_worker=False,
            rate_limit_rpm=30,
            signup_rate_per_min=1,
            signup_burst=1,
        )
    )
    with TestClient(app) as client:
        first = client.post(
            "/v1/auth/signup",
            json={"email": "first@example.com"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        second = client.post(
            "/v1/auth/signup",
            json={"email": "second@example.com"},
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        assert first.status_code == 201
        assert second.status_code == 429
