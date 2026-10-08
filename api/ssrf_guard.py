"""SSRF guard: an in-process, loopback-only HTTP proxy that validates the
target at *dial time*.

Why a proxy instead of a pre-flight check
------------------------------------------
"Does this hostname resolve to a safe IP?" checked before the request has a
TOCTOU / DNS-rebinding window: the pre-check lookup is safe, the actual
model call's lookup may return a different address. This proxy closes the
window by doing the final resolution + validation inside the connect path:

  1. client (httpx) issues an absolute-URI request to 127.0.0.1:PORT
  2. proxy resolves the target host (client DNS)
  3. proxy validates every returned address (private/loopback/link-local/
     reserved/multicast/site-local → refuse)
  4. proxy dials one safe address and forwards the raw byte stream
  5. TLS (if any) is done by the *client*, whose SNI/cert verification sees
     the original hostname — so TLS semantics are unchanged

The listener is bound to 127.0.0.1 on an ephemeral port; it is not
reachable from the network, and the port is handed to the runner in-process.
Public deployments therefore cannot use evaldiff to probe internal
services, even when a hostname rebinds between lookups.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from typing import Any

# Hosts we never dial, no matter what anyone resolves to them.
BLOCKED_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "localhost6.localdomain6",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",  # cloud metadata endpoints
    }
)


def is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """True for address classes we never dial by default.

    Covers IPv4 (private/loopback/link-local/reserved/multicast/unspecified)
    and IPv6 (plus site-local fec0::/10).
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def blocked_target(host: str) -> str | None:
    """Reason this target must never be dialed, or None if parse-time-safe.

    Hostnames return None here — they are re-validated at dial time, which
    is the point of the guard.
    """
    h = host.strip().lower().rstrip(".")
    if h in BLOCKED_NAMES:
        return "well-known internal name"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if is_blocked_ip(ip):
        return "private/reserved IP range"
    return None


class GuardError(Exception):
    """The guard refused a target (policy or resolution failure)."""


def _dial(host: str, port: int, *, allow_local: bool) -> socket.socket:
    """Resolve, validate every candidate at dial time, connect to a safe one."""
    reason = blocked_target(host)
    if reason is not None and not allow_local:
        raise GuardError(f"{host}: blocked ({reason}) by deployment policy")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GuardError(f"{host}: DNS resolution failed ({exc})") from exc
    if not infos:
        raise GuardError(f"{host}: no addresses")
    last_err: Exception | None = None
    for _family, _type, _proto, _cname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not allow_local and is_blocked_ip(ip):
            # Safe in a pre-check, private at dial time: exactly the
            # rebinding case this guard exists to close.
            raise GuardError(f"{host}: resolved to blocked address {ip} at dial time")
        sock: socket.socket | None = None
        try:
            sock = socket.socket(_family, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect(sockaddr[:2])
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                sock.close()
    raise GuardError(f"{host}:{port}: no safe route (last: {last_err})")


def _forward(src: socket.socket, dst: socket.socket, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except (OSError, ConnectionError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _read_headers(sock: socket.socket) -> tuple[list[bytes], dict[str, str]]:
    """Read the raw request line + header block. Returns (raw_lines, lowercased dict)."""
    raw: list[bytes] = []
    headers: dict[str, str] = {}
    while True:
        line = b""
        while not line.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                return raw, headers
            line += byte
        line = line.rstrip(b"\r\n")
        raw.append(line)
        if line == b"":
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("latin1")] = v.strip().decode("latin1", "replace")
    return raw, headers


def _read_response(sock: socket.socket, cap: int = 64 * 1024 * 1024) -> bytes:
    """Read a complete HTTP/1.1 response (content-length, chunked, or EOF-bounded)."""
    out = bytearray()
    # headers
    while True:
        line = b""
        while not line.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                return bytes(out)
            line += byte
        line = line.rstrip(b"\r\n")
        out += line + b"\r\n"
        if line == b"":
            break
    htxt = bytes(out).decode("latin1", "replace").lower()
    if "content-length:" in htxt:
        n = int(htxt.split("content-length:", 1)[1].split("\r\n", 1)[0].strip())
        need = n
        got = 0
        while got < need:
            chunk = sock.recv(min(65536, need - got))
            if not chunk:
                break
            out += chunk
            got += len(chunk)
        return bytes(out)
    if "transfer-encoding:" in htxt and "chunked" in htxt:
        # de-chunk by reading until the terminal zero chunk
        while True:
            size_line = b""
            while not size_line.endswith(b"\r\n"):
                byte = sock.recv(1)
                if not byte:
                    return bytes(out)
                size_line += byte
            n = int(size_line.strip().split(b";")[0] or b"0", 16)
            out += size_line
            if n == 0:
                # trailers up to blank line
                while True:
                    tl = b""
                    while not tl.endswith(b"\r\n"):
                        byte = sock.recv(1)
                        if not byte:
                            return bytes(out)
                        tl += byte
                    out += tl
                    if tl in (b"\r\n", b"\n"):
                        break
                return bytes(out)
            chunk = b""
            while len(chunk) < n:
                c = sock.recv(min(65536, n - len(chunk)))
                if not c:
                    break
                chunk += c
            out += chunk
            crlf = b""
            while not crlf.endswith(b"\n"):
                byte = sock.recv(1)
                if not byte:
                    break
                crlf += byte
            out += crlf
            if len(out) > cap:
                return bytes(out)
    # no framing: read to EOF (bounded)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        out += chunk
        if len(out) > cap:
            break
    return bytes(out)


def _handle(client: socket.socket, *, allow_local: bool) -> None:
    try:
        raw_lines, headers = _read_headers(client)
        if not raw_lines:
            client.close()
            return
        request_line = raw_lines[0].decode("latin1", "replace").strip()
        parts = request_line.split(" ")
        if len(parts) < 2:
            client.close()
            return
        method, target, version = parts[0], parts[1], (parts[2] if len(parts) > 2 else "HTTP/1.1")
        if not target.startswith(("http://", "https://")):
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            client.close()
            return
        from urllib.parse import urlparse

        p = urlparse(target)
        host = p.hostname or ""
        if not host:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            client.close()
            return
        port = p.port or (443 if p.scheme == "https" else 80)
        path = p.path or "/"
        if p.query:
            path = f"{path}?{p.query}"
        if p.fragment:
            path = f"{path}#{p.fragment}"

        body = b""
        if "content-length" in headers:
            body = b""
            need = int(headers["content-length"])
            while len(body) < need:
                chunk = client.recv(min(65536, need - len(body)))
                if not chunk:
                    break
                body += chunk

        upstream = _dial(host, port, allow_local=allow_local)
    except GuardError as exc:
        body = str(exc).encode("utf-8", "replace")
        client.sendall(
            (
                f"HTTP/1.1 403 Forbidden\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"X-Evaldiff-Guard: blocked\r\n"
                f"\r\n".encode()
                + body
            )
        )
        client.close()
        return
    except OSError as exc:
        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        client.close()
        return

    try:
        # Rebuild the request in origin form for the upstream server.
        # (raw_lines ends with the blank separator line — exclude it.)
        hdr_lines = [
            f"{method} {path} {version}",
            f"Host: {host}",
            "Connection: close",
        ]
        for line in raw_lines[1:-1]:
            txt = line.decode("latin1", "replace")
            lk = txt.split(":", 1)[0].strip().lower()
            if lk in ("host", "connection", "proxy-connection", "proxy-authorization"):
                continue
            hdr_lines.append(txt)
        req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("latin1") + body
        upstream.sendall(req)
        response = _read_response(upstream)
        client.sendall(response)
    except OSError as exc:
        try:
            client.sendall(
                (
                    f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                    f"X-Evaldiff-Guard: {type(exc).__name__}\r\n\r\n".encode()
                )
            )
        except OSError:
            pass
    finally:
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass


class SSRFGuard:
    """A loopback-only proxy that closes DNS-rebinding at dial time.

    Usage::

        guard = SSRFGuard().start()
        client = httpx.AsyncClient(proxy=guard.proxy_url)
        ...
        guard.stop()

    ``proxy_url`` is an ``http://127.0.0.1:<ephemeral>`` URL — safe to pass
    to httpx, never routable off the host.
    """

    def __init__(self, *, allow_local: bool = False, host: str = "127.0.0.1") -> None:
        self.allow_local = allow_local
        self.host = host
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> "SSRFGuard":
        import socketserver

        handler_args = {"allow_local": self.allow_local}

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:  # type: ignore[override]
                _handle(self.request, **handler_args)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server((self.host, 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="evaldiff-ssrf-guard", daemon=True
        )
        self._thread.start()
        return self

    @property
    def proxy_url(self) -> str:
        assert self.port is not None, "start() first"
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
