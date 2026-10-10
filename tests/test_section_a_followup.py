"""Section A follow-up: close the three reviewer-found gaps.

1. Decompression allocation must be bounded *during* decoding (a
   decompression bomb must raise at the cap, not allocate the expansion).
2. A client that stops reading must not leave the proxy buffering the
   upstream response (proxy thread + upstream socket must be released
   promptly), and a stalled upstream must time out the proxy's own read.
3. Proxy-detected size violations (tagged 502s) must surface as
   ResponseLimitExceeded — recorded, not charged, NOT retried.
"""

from __future__ import annotations

import gzip
import socket
import threading
import time
import zlib
from typing import Self

import httpx
import pytest

import api.http_limits as hl
from api.db import state
from api.settings import Settings
from tests.conftest import _free_port


def _set_limits(max_bytes: int, idle: float) -> None:
    state.settings = Settings(
        database_url="sqlite://",
        enable_worker=False,
        rate_limit_rpm=0,
        signup_rate_per_min=0,
        response_max_bytes=max_bytes,
        response_idle_timeout=idle,
    )


def test_decompression_bomb_is_capped_without_allocating_expansion() -> None:
    """A 2 KiB cap must stop an 8 MiB expansion with peak allocation far
    below the expanded size (windowed incremental decompression)."""
    _set_limits(2048, 5.0)

    peak = {"bytes": 0}

    class TrackingDecoder:
        def __init__(self, inner) -> None:
            self._inner = inner

        def decompress(self, data: bytes, max_out: int) -> bytes:
            out = self._inner.decompress(data, max_out)
            peak["bytes"] = max(peak["bytes"], len(out))
            return out

        def flush(self) -> bytes:
            return b""

    real_decoder_for = hl._decoder_for

    def tracking(encoding: str):
        d = real_decoder_for(encoding)
        return TrackingDecoder(d) if d is not None else None

    hl._decoder_for = tracking
    try:
        expanded = b"payload-" + b"x" * (8 * 1024 * 1024)
        wire = gzip.compress(expanded)
        assert len(wire) < 64 * 1024  # the bomb: tiny on the wire

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, stream=httpx.ByteStream(wire), headers={"Content-Encoding": "gzip"}
            )

        async def go() -> None:
            with pytest.raises(hl.ResponseLimitExceeded):
                await hl.post_json_bounded(
                    httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                    "https://model.example/chat/completions",
                    body={},
                    headers={},
                    timeout=5.0,
                )

        asyncio.run(go())
    finally:
        hl._decoder_for = real_decoder_for
    # Peak per-call allocation stayed in the window (64 KiB) + slack — never
    # anywhere near the 8 MiB expansion.
    assert peak["bytes"] <= 64 * 1024 * 2, f"unbounded decompression: {peak['bytes']}"


def test_gzip_within_cap_is_decoded_correctly() -> None:
    _set_limits(65536, 5.0)
    payload = b'{"choices": [{"message": {"content": "ok"}}]}'
    wire = gzip.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(wire), headers={"Content-Encoding": "gzip"})

    async def go() -> dict:
        return await hl.post_json_bounded(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "https://model.example/chat/completions",
            body={},
            headers={},
            timeout=5.0,
        )

    data = asyncio.run(go())
    assert data["choices"][0]["message"]["content"] == "ok"


def test_raw_deflate_within_cap_is_decoded() -> None:
    _set_limits(65536, 5.0)
    payload = b'{"choices": [{"message": {"content": "deflate"}}]}'
    co = zlib.compressobj(6, zlib.DEFLATED, -15)  # raw deflate (no zlib header)
    wire = co.compress(payload) + co.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(wire), headers={"Content-Encoding": "deflate"})

    async def go() -> dict:
        return await hl.post_json_bounded(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "https://model.example/chat/completions",
            body={},
            headers={},
            timeout=5.0,
        )

    data = asyncio.run(go())
    assert data["choices"][0]["message"]["content"] == "deflate"


def test_proxy_size_violation_becomes_limit_not_retryable() -> None:
    """The proxy's own cap surfaces as a tagged 502; the bounded reader
    must translate it to ResponseLimitExceeded so _with_retries (which only
    retries httpx.TransportError / HTTPStatusError subclasses) never
    re-issues the request."""
    from api import runner

    _set_limits(65536, 5.0)
    handler_called = {"n": 0}

    def proxy502(request: httpx.Request) -> httpx.Response:
        handler_called["n"] += 1
        return httpx.Response(
            502,
            headers={"X-Evaldiff-Guard": "ResponseTooLarge"},
            content=b"upstream response exceeds size limit",
        )

    async def go() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(proxy502))
        with pytest.raises(hl.ResponseLimitExceeded):
            await runner._with_retries(
                hl.post_json_bounded,
                client,
                "https://model.example/chat/completions",
                body={},
                headers={},
                timeout=5.0,
                attempts=3,
            )

    asyncio.run(go())
    # A retry would have called the endpoint again — it must not.
    assert handler_called["n"] == 1


def test_proxy_stall_violation_becomes_stalled_not_retryable() -> None:
    _set_limits(65536, 5.0)
    calls = {"n": 0}

    def proxy502(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            502,
            headers={"X-Evaldiff-Guard": "UpstreamStalled"},
            content=b"upstream stalled",
        )

    async def go() -> None:
        from api import runner

        client = httpx.AsyncClient(transport=httpx.MockTransport(proxy502))
        with pytest.raises(hl.ResponseStalled):
            await runner._with_retries(
                hl.post_json_bounded,
                client,
                "https://model.example/chat/completions",
                body={},
                headers={},
                timeout=5.0,
                attempts=3,
            )

    asyncio.run(go())
    assert calls["n"] == 1


def test_plain_502_is_still_retryable() -> None:
    """Untagged 502s (genuine gateway trouble) keep the old behavior:
    retried, and the final exception is the httpx status error."""
    _set_limits(65536, 5.0)
    calls = {"n": 0}

    def proxy502(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, content=b"boom")

    async def go() -> None:
        from api import runner

        client = httpx.AsyncClient(transport=httpx.MockTransport(proxy502))
        with pytest.raises(httpx.HTTPStatusError):
            await runner._with_retries(
                hl.post_json_bounded,
                client,
                "https://model.example/chat/completions",
                body={},
                headers={},
                timeout=5.0,
                attempts=2,
                base_delay=0.01,
            )

    asyncio.run(go())
    assert calls["n"] == 2


def test_proxy_releases_upstream_when_client_disappears() -> None:
    """Client closes mid-response; the proxy must stop buffering and close
    the upstream socket within ~the idle window (not pin it for 120 s)."""
    import socket as _socket

    import api.ssrf_guard as g

    port = _free_port()
    server = _socket.socket()
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)

    holder_state = {"conn": None}

    def run() -> None:
        conn, _ = server.accept()
        holder_state["conn"] = conn
        # Pre-fill the send buffer: headers + 1 KiB of a declared 1 MiB
        # body. The rest is never sent — the holder just holds the socket.
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 1048576\r\n"
            b"\r\n" + b"y" * 1024
        )
        conn.settimeout(None)
        try:
            while True:
                conn.recv(1)  # blocks; woken by EOF when the proxy closes
        except OSError:
            pass

    holder = threading.Thread(target=run, daemon=True)
    holder.start()
    client_sock, proxy_sock = _socket.socketpair()
    g.UPSTREAM_IDLE_TIMEOUT = 1.0
    handler = None
    try:
        with mock_dial(target=f"127.0.0.1:{port}"):
            handler = threading.Thread(
                target=lambda: g._handle(proxy_sock, allow_local=True, stop=threading.Event()),
                daemon=True,
            )
            handler.start()
            client_sock.sendall(
                b"GET http://model.example/ HTTP/1.1\r\nHost: model.example\r\n\r\n"
            )
            # The client takes a partial response, then gives up — while
            # the proxy is still reading the declared 1 MiB body.
            client_sock.settimeout(3.0)
            try:
                while True:
                    chunk = client_sock.recv(65536)
                    if not chunk:
                        break
            except TimeoutError:
                pass
            client_sock.close()
            handler.join(timeout=5.0)
        assert not handler.is_alive(), "proxy handler did not terminate (thread pinned)"
        # The upstream socket must have been closed by the proxy: the
        # holder's recv gets EOF (or the connection is already gone).
        conn = holder_state["conn"]
        assert conn is not None
        conn.settimeout(2.0)
        got_eof = False
        while True:
            try:
                chunk = conn.recv(65536)
            except (TimeoutError, OSError):
                break
            if not chunk:
                got_eof = True
                break
        assert got_eof, "proxy did not release (close) the upstream socket"
    finally:
        g.UPSTREAM_IDLE_TIMEOUT = 30.0
        holder.join(timeout=1)
        for s in (client_sock, proxy_sock, server):
            try:
                s.close()
            except OSError:
                pass


def test_proxy_upstream_stall_becomes_tagged_502() -> None:
    """Upstream sends nothing after the headers: the proxy's idle window
    must fire and the client gets a tagged 502 (not a 120 s hang)."""
    port = _free_port()

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)

    def run() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
        while True:
            time.sleep(1)

    holder = threading.Thread(target=run, daemon=True)
    holder.start()
    client_sock, proxy_sock = socket.socketpair()
    import api.ssrf_guard as g

    g.UPSTREAM_IDLE_TIMEOUT = 1.0
    handler = None
    try:
        with mock_dial(target=f"127.0.0.1:{port}"):
            # Run the handler in its own thread: it blocks on recv() before
            # the request is even sent.
            t0 = time.monotonic()
            handler = threading.Thread(
                target=lambda: g._handle(proxy_sock, allow_local=True, stop=threading.Event()),
                daemon=True,
            )
            handler.start()
            client_sock.sendall(b"GET http://model.example/ HTTP/1.1\r\nHost: model.example\r\n\r\n")
            handler.join(timeout=5.0)
            elapsed = time.monotonic() - t0
            client_sock.settimeout(2.0)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = client_sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except TimeoutError:
                pass
        assert not handler.is_alive(), "proxy handler did not terminate (thread pinned)"
        assert elapsed < 4.0
        assert data.startswith(b"HTTP/1.1 502")
        assert b"X-Evaldiff-Guard: UpstreamStalled" in data
    finally:
        g.UPSTREAM_IDLE_TIMEOUT = 30.0
        holder.join(timeout=1)
        for s in (client_sock, proxy_sock):
            try:
                s.close()
            except OSError:
                pass


class mock_dial:
    """Patch SSRFGuard._dial to a pre-connected socket (or target)."""

    def __init__(self, upstream_sock: socket.socket | None = None, target: str | None = None) -> None:
        self.upstream_sock = upstream_sock
        self.target = target
        self._orig = None

    def __enter__(self) -> Self:
        import api.ssrf_guard as g

        self._orig = g._dial

        def fake(host: str, port: int, *, allow_local: bool) -> socket.socket:
            if self.upstream_sock is not None:
                return self.upstream_sock
            assert self.target is not None
            h, p = self.target.split(":")
            return self._orig(h, int(p), allow_local=allow_local)

        g._dial = fake
        return self

    def __exit__(self, *exc) -> None:
        import api.ssrf_guard as g

        g._dial = self._orig


import asyncio
