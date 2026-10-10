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

import ipaddress
import select
import socket
import threading
import time
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


def _tunnel(client: socket.socket, upstream: socket.socket, stop: threading.Event) -> None:
    """Relay TLS bytes without terminating TLS or changing certificate checks."""
    while not stop.is_set():
        readable, _, _ = select.select([client, upstream], [], [], 0.5)
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            destination = upstream if source is client else client
            destination.sendall(data)


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


class ResponseTooLarge(OSError):
    """The upstream HTTP response exceeded the buffering limit."""


class UpstreamStalled(OSError):
    """The upstream stopped sending data before completing the response."""


class ClientDisconnected(OSError):
    """The client went away while the upstream response was being buffered."""


UPSTREAM_IDLE_TIMEOUT = 30.0  # seconds of no upstream data while buffering


def _client_gone(client: socket.socket) -> bool:
    """True if the client closed the connection (without consuming its data).

    A readable client with buffered data is pipelining the next request on
    the same connection — alive. MSG_PEEK so we never eat that data.
    """
    try:
        readable, _, _ = select.select([client], [], [], 0)
        if not readable:
            return False
        return client.recv(16384, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _read_response(
    sock: socket.socket,
    cap: int = 64 * 1024 * 1024,
    idle: float = UPSTREAM_IDLE_TIMEOUT,
    client: socket.socket | None = None,
) -> bytes:
    """Read a framed response, bounding headers, body, and trailers together.

    Three independent bounds:
    - ``cap``: total bytes (headers + body + trailers) that may be buffered —
      a response that fits exactly at ``cap`` is still returned in full;
    - ``idle``: if the upstream delivers no bytes within this window, stop
      reading and raise ``UpstreamStalled``. Without this, a client that
      gave up leaves the handler blocked in ``recv`` for the full socket
      timeout, pinning a thread and both sockets per stalled case;
    - ``client``: if provided, the handler wakes on a 0.25 s tick and checks
      whether the client closed its end (``_client_gone``); if so it stops
      buffering and raises ``ClientDisconnected`` — otherwise the disconnect
      is only noticed when the final ``sendall`` fails, i.e. *after* the
      whole upstream body has been read and the sockets pinned.

    The reader is a small state machine over a single growing buffer, so
    EOF (or hitting the cap) is always evaluated against *how much of the
    frame has been consumed*: a read-until-EOF body ending in EOF is
    complete by protocol, while EOF mid-frame (chunked / content-length)
    means the server closed early and is rejected.

    Sockets without a file descriptor (test fakes) fall back to a plain
    blocking ``recv`` — the cap still applies.
    """
    buf = bytearray()
    deadline = time.monotonic() + idle if idle > 0 else None

    def _client_gone_now() -> bool:
        return client is not None and _client_gone(client)

    def receive(n: int) -> int:
        """Read up to ``n`` bytes into ``buf``; returns bytes read (0 on EOF)."""
        nonlocal deadline
        while True:
            try:
                if client is not None:
                    r, _, _ = select.select([sock, client], [], [], 0.25)
                else:
                    r, _, _ = select.select([sock], [], [], 0.25)
            except (OSError, ValueError, TypeError):
                # No selectable fd (test fakes / closed): plain blocking
                # recv. The cap below still bounds the buffer.
                data = sock.recv(n)
            else:
                if client is not None and client in r and _client_gone(client):
                    raise ClientDisconnected("client disconnected before response complete")
                if sock not in r:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise UpstreamStalled(
                            f"upstream stalled: no data for {idle}s (read {len(buf)} bytes so far)"
                        )
                    continue
                try:
                    data = sock.recv(n)
                except TimeoutError:  # settimeout backstop fired
                    if _client_gone_now():
                        raise ClientDisconnected(
                            "client disconnected before response complete"
                        ) from None
                    if deadline is not None and time.monotonic() >= deadline:
                        raise UpstreamStalled(
                            f"upstream stalled: no data for {idle}s (read {len(buf)} bytes so far)"
                        ) from None
                    continue
            if not data:
                return 0
            if len(buf) + len(data) > cap:
                raise ResponseTooLarge("upstream response exceeds size limit")
            buf.extend(data)
            if deadline is not None:
                deadline = time.monotonic() + idle
            return len(data)

    def eof_ok(
        state: str,
        idx: int,
        headers: dict[bytes, bytes],
        size: int,
        remaining: int,
    ) -> bool:
        """True iff EOF at this point of the frame is a *complete* response."""
        if state == "done":
            return True
        if state == "ebody":
            # Read-until-EOF body: EOF *is* the completion signal.
            return True
        if state == "hdr":
            return False  # unterminated header block
        if state == "csize":
            return False  # waiting for a chunk size
        if state == "cdata":
            return False  # chunk data incomplete
        if state == "ccrlf":
            return False  # chunk CRLF incomplete
        if state == "ctrail":
            # Trailer section: complete only if the terminating blank line
            # has been seen — tracked by the state machine reaching "done".
            return False
        if state == "cbody":
            return remaining == 0
        return False

    # --- state machine over ``buf`` ---
    # states: hdr | csize | cdata | ccrlf | ctrail | cbody | ebody | done
    state = "hdr"
    idx = 0
    size = 0
    remaining = 0
    headers: dict[bytes, bytes] = {}
    header_done = False

    while True:
        if state == "done":
            break
        if state == "hdr" and not header_done:
            nl = buf.find(b"\n", idx)
            if nl < 0:
                # No terminator yet: EOF here means incomplete headers.
                if not receive(1) and not eof_ok(state, idx, headers, size, remaining):
                    raise OSError("incomplete upstream response")
                continue
            prev = buf.rfind(b"\n", 0, nl)
            line = bytes(buf[prev + 1 : nl])
            idx = nl + 1
            if line in (b"", b"\r"):  # blank separator line
                header_done = True
                if b"chunked" in headers.get(b"transfer-encoding", b""):
                    state = "csize"
                elif b"content-length" in headers:
                    try:
                        remaining = int(headers[b"content-length"])
                    except ValueError:
                        raise OSError("invalid upstream response length") from None
                    # Declared body cannot fit under the cap: reject before
                    # the server has to send a single body byte.
                    if idx + remaining > cap:
                        raise ResponseTooLarge("upstream response exceeds size limit")
                    state = "cbody" if remaining else "done"
                else:
                    state = "ebody"
            else:
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers[k.strip().lower()] = v.strip().lower()
            continue
        if state == "csize":
            nl = buf.find(b"\n", idx)
            if nl < 0:
                if not receive(1) and not eof_ok(state, idx, headers, size, remaining):
                    raise OSError("incomplete upstream response")
                continue
            try:
                size = int(bytes(buf[idx:nl]).strip().split(b";")[0], 16)
            except ValueError:
                raise OSError("invalid upstream response length") from None
            # A declared chunk that cannot fit under the cap is over the
            # limit regardless of how much the server actually sends.
            if idx + size + 2 > cap:
                raise ResponseTooLarge("upstream response exceeds size limit")
            idx = nl + 1
            state = "cdata" if size else "ctrail"
            continue
        if state == "cdata":
            if idx + size > len(buf):
                if not receive(min(65536, idx + size - len(buf))) and not eof_ok(
                    state, idx, headers, size, remaining
                ):
                    raise OSError("incomplete upstream response")
                continue
            idx += size
            state = "ccrlf"
            continue
        if state == "ccrlf":
            if idx + 2 > len(buf):
                if not receive(2) and not eof_ok(state, idx, headers, size, remaining):
                    raise OSError("incomplete upstream response")
                continue
            if bytes(buf[idx : idx + 2]) != b"\r\n":
                raise OSError("invalid chunk terminator")
            idx += 2
            state = "csize" if size else "ctrail"
            continue
        if state == "ctrail":
            nl = buf.find(b"\n", idx)
            if nl < 0:
                if not receive(1) and not eof_ok(state, idx, headers, size, remaining):
                    raise OSError("incomplete upstream response")
                continue
            prev = buf.rfind(b"\n", 0, nl)
            line = bytes(buf[prev + 1 : nl])
            idx = nl + 1
            if line in (b"", b"\r"):  # terminating blank line
                state = "done"
                break
            continue
        if state == "cbody":
            if remaining <= 0:
                state = "done"
                break
            if idx + remaining > len(buf):
                if not receive(min(65536, idx + remaining - len(buf))) and not eof_ok(
                    state, idx, headers, size, remaining
                ):
                    raise OSError("incomplete upstream response")
                continue
            idx += remaining
            remaining = 0
            continue
        if state == "ebody":
            if receive(65536) == 0:
                state = "done"
                break
            idx = len(buf)
            continue
        raise AssertionError(f"unhandled state {state!r}")

    return bytes(buf[:idx])


def _handle(client: socket.socket, *, allow_local: bool, stop: threading.Event) -> None:
    client.settimeout(120)
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
        from urllib.parse import urlsplit

        is_connect = method == "CONNECT"
        p = urlsplit("//" + target if is_connect else target)
        if (not is_connect and p.scheme != "http") or p.username or p.password:
            raise ValueError("invalid proxy target")
        host = p.hostname or ""
        port = p.port or (443 if is_connect else 80)
        if not host or (is_connect and (p.path or p.query or p.fragment)):
            raise ValueError("invalid proxy authority")
        path = p.path or "/"
        if p.query:
            path = f"{path}?{p.query}"

        body = b""
        if "content-length" in headers:
            body = b""
            need = int(headers["content-length"])
            while len(body) < need:
                if _client_gone(client):
                    client.close()
                    return
                chunk = client.recv(min(65536, need - len(body)))
                if not chunk:
                    break
                body += chunk

        upstream = _dial(host, port, allow_local=allow_local)
    except GuardError as exc:
        body = str(exc).encode("utf-8", "replace")
        client.sendall(
            f"HTTP/1.1 403 Forbidden\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Evaldiff-Guard: blocked\r\n"
            f"\r\n".encode()
            + body
        )
        client.close()
        return
    except ValueError:
        client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        client.close()
        return
    except OSError:
        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        client.close()
        return

    try:
        upstream.settimeout(120)
        if is_connect:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _tunnel(client, upstream, stop)
            return
        # Rebuild the request in origin form for the upstream server.
        # (raw_lines ends with the blank separator line — exclude it.)
        hdr_lines = [
            f"{method} {path} {version}",
            f"Host: {p.netloc}",
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
        # Pass the client so a disconnect is observed *while* the response
        # is buffered (previously it was only noticed when sendall below
        # failed — i.e. after the whole upstream body had been read).
        # idle is read from the module constant at call time so tests can
        # shorten the window.
        response = _read_response(upstream, client=client, idle=UPSTREAM_IDLE_TIMEOUT)
        client.sendall(response)
    except ClientDisconnected:
        # The client is gone: no response to deliver, just release both
        # sockets so the stalled thread + upstream connection don't leak.
        pass
    except OSError as exc:
        # Includes ResponseTooLarge (proxy cap) and UpstreamStalled (proxy
        # read window) — both surface as tagged 502s that the client-side
        # bounded reader translates into non-retryable limit errors.
        try:
            client.sendall(
                f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                f"X-Evaldiff-Guard: {type(exc).__name__}\r\n\r\n".encode()
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
        self._stop = threading.Event()
        self.allow_local = allow_local
        self.host = host
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> SSRFGuard:
        import socketserver

        self._stop.clear()
        handler_args = {"allow_local": self.allow_local, "stop": self._stop}

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
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
