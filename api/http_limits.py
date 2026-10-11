"""Shared bounded JSON POST for model and judge calls (NEEDS-DOING A).

Closes the unbounded response paths left after the SSRF guard. Both the
model path (``api.runner.call_model``) and the judge path
(``api.judges.rubric_llm``) read their response through
``post_json_bounded``, so the limits apply to both, over plain HTTP and
over HTTPS (the SSRF-guard CONNECT tunnel):

- **byte cap** on the *decoded* body (16 MiB default,
  ``EVALDIFF_RESPONSE_MAX_BYTES``);
- **idle timeout** between data chunks — a slow-drip upstream under the cap
  must not pin a worker (``EVALDIFF_RESPONSE_IDLE_TIMEOUT``, 10 s default);
- **stream close** on cap/stall/error — tearing the response down tears the
  proxy tunnel down with it, so no long-lived connection survives a
  violation.

Memory safety on compression
----------------------------
We do not use ``response.aiter_bytes()``: httpx's decoder allocates a
chunk's full decompressed output *before* any limit can be checked, so a
32 KiB gzip payload expanding to 32 MiB allocates tens of MiB and only
then trips the cap. Instead we request ``Accept-Encoding: identity`` (the
server sends the body uncompressed) and, as defense in depth against a
server that ignores it, decode **incrementally with a per-call output
window** (``zlib.decompressobj().decompress(data, max_out)``). Each call
can emit at most 64 KiB bytes, so the total allocation at any moment is
bounded by ``len(out)`` + one window + one raw chunk — never by the
expansion ratio. A decompression bomb raises ``ResponseLimitExceeded``
once the window crosses the cap.

Supported encodings: identity, gzip, deflate (zlib-wrapped or raw
stream), and br (when the optional ``brotli`` package is installed and
bounded); anything else is rejected with ``ResponseLimitExceeded`` — we
asked for identity, so a server that compresses with a codec we cannot
bound is not a response we can safely accept.

Proxy cooperation
-----------------
The SSRF proxy observes client disconnects and an upstream idle window
while buffering the response (``api.ssrf_guard``), and tags its own
violations: a 502 carrying ``X-Evaldiff-Guard: ResponseTooLarge`` (proxy
cap) or ``UpstreamStalled`` (proxy read timeout). This module translates
those markers into ``ResponseLimitExceeded`` / ``ResponseStalled`` so they
are recorded, not charged, and — critically — NOT retried by
``_with_retries``.

``ResponseLimitExceeded`` / ``ResponseStalled`` are deliberately NOT
``httpx.TransportError``: a deterministic size violation or a dead
upstream is not going to get better on retry, and the failed case is not
charged (``_execute_cases`` records the error and skips billing).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import zlib
from typing import Any, NoReturn

import httpx

DEFAULT_RESPONSE_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB decoded body
DEFAULT_RESPONSE_IDLE_TIMEOUT = 10.0  # seconds of no data => stalled
_DECOMPRESSION_WINDOW = 64 * 1024  # max bytes one decompress call may emit
_GUARD_HEADER = "x-evaldiff-guard"  # set by api.ssrf_guard on its 502s


class ResponseLimitExceeded(OSError):
    """The decoded response body exceeded the configured byte cap."""


class ResponseStalled(OSError):
    """The upstream stopped sending data before completing the body."""


def _limits() -> tuple[int, float]:
    from .db import state

    settings = state.settings
    if settings is None:
        return DEFAULT_RESPONSE_MAX_BYTES, DEFAULT_RESPONSE_IDLE_TIMEOUT
    max_bytes = getattr(settings, "response_max_bytes", None)
    idle = getattr(settings, "response_idle_timeout", None)
    return (
        int(max_bytes) if max_bytes is not None else DEFAULT_RESPONSE_MAX_BYTES,
        float(idle) if idle is not None else DEFAULT_RESPONSE_IDLE_TIMEOUT,
    )


async def _aclose(response: httpx.Response) -> None:
    with contextlib.suppress(Exception):
        await response.aclose()


class _ZlibDecoder:
    """A zlib-family stream (gzip or zlib-wrapped) with the uniform
    bounded-decoding protocol: ``decompress(data, max_out)`` + ``residual``
    (input passed in but not yet decoded — the caller must feed it back)."""

    def __init__(self, wbits: int) -> None:
        self._d = zlib.decompressobj(wbits)

    def decompress(self, data: bytes, max_out: int) -> bytes:
        return self._d.decompress(data, max_out)

    @property
    def residual(self) -> bytes:
        return self._d.unconsumed_tail

    def flush(self) -> bytes:
        return self._d.flush()


class _RawDeflateDecoder:
    """``Content-Encoding: deflate``, whose wire form is ambiguous
    (zlib-wrapped vs raw deflate stream).

    The format can only be settled from the bytes themselves, and it must
    not depend on where the network chunk boundaries fall. Until it is
    settled, every wire chunk is fed to **two stateful probes in
    parallel** — a zlib-wrapped decoder (``wbits=15``) and a raw-deflate
    decoder (``wbits=-15``):

    - a probe that raises is dead: a *valid* deflate stream (either form)
      fed to a stateful zlib decoder never raises on incomplete input, it
      holds it, so a raise is conclusive evidence the format is not this
      one;
    - a probe that reaches end-of-stream has decoded the whole body
      cleanly — that interpretation wins;
    - a probe that never settles (still incomplete) and a body that
      exhausts the detection buffer (``max_bytes`` — a body cannot
      legally exceed the cap — or the decode window, whichever is larger)
      is rejected. This is what bounds the pre-commit buffer: it can never
      grow past ``detection buffer + one window``.

    Probing is stateful — each wire byte is decoded by each live probe
    exactly once — so detection is linear in wire bytes, not O(n²) in
    re-decoding the accumulated prefix. Committing on *positive output
    alone* is unsound (a stream of empty stored blocks is valid raw
    deflate that decodes to nothing, and would buffer forever); committing
    on *clean end-of-stream or a dead rival* is sound.
    """

    _WINDOW = 64 * 1024  # max bytes a single decompress call may emit

    def __init__(self, cap: int = 0) -> None:
        self._active: Any = None  # the committed zlib.decompressobj
        self._committed = False
        # The two live probes (one per interpretation), each stateful.
        self._z: Any = zlib.decompressobj(15)
        self._r: Any = zlib.decompressobj(-zlib.MAX_WBITS)
        self._z_dead = self._r_dead = False
        # Decoded output each probe has produced so far. Only the committed
        # probe's buffer is ever returned; the rival's is discarded on settle.
        self._z_buf = bytearray()
        self._r_buf = bytearray()
        self._wire_seen = 0  # total wire bytes fed while unsettled
        self._cap = cap

    def _detection_buffer(self) -> int:
        """Largest wire size a *valid* body that decodes to <= ``cap`` bytes
        can have, plus one decode window of in-flight input.

        Deflate's worst case is stored blocks: 5 bytes of overhead per
        65535 of payload, plus the zlib header/ADLER (a constant). A body
        whose wire size exceeds this cannot decode to <= ``cap`` without
        being malformed, so it is rejected — this bounds the pre-settle
        buffering while still accepting every legal body.
        """
        base = self._cap if self._cap > 0 else self._WINDOW
        return base + (base // 65535 + 1) * 5 + self._WINDOW + 16

    def _probe(self, d: Any, data: bytes) -> tuple[bytes, bool]:
        """Feed ``data`` to one stateful probe; return (output, dead)."""
        try:
            return d.decompress(data, self._WINDOW), False
        except zlib.error:
            return b"", True

    def _reject(self, msg: str) -> NoReturn:
        self._z_buf = self._r_buf = b""
        self._z = self._r = None
        raise ResponseLimitExceeded(msg)

    def decompress(self, data: bytes, max_out: int) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.decompress(data, max_out)
        if not data:
            return b""
        self._wire_seen += len(data)
        if not self._z_dead:
            out, dead = self._probe(self._z, data)
            self._z_dead = dead
            self._z_buf += out
        if not self._r_dead:
            out, dead = self._probe(self._r, data)
            self._r_dead = dead
            self._r_buf += out
        # A probe that decoded more than ``cap`` bytes is a bomb (a legal
        # body decodes to <= cap): reject it before settling.
        if self._cap > 0 and (len(self._z_buf) > self._cap or len(self._r_buf) > self._cap):
            self._reject(f"deflate body exceeds limit: {self._cap} bytes")
        # Settle the format on conclusive evidence:
        #  1. A probe reached end-of-stream -> it decoded the whole body
        #     cleanly; commit it immediately. Committing at eof (rather
        #     than waiting for output) is what keeps buffering bounded:
        #     a valid stream of empty stored blocks reaches eof and
        #     commits with zero output instead of buffering forever.
        #  2. One probe is dead and the other produced output -> the live
        #     deflate decoder never raises on incomplete input, so a raise
        #     is conclusive that the format is not the dead probe's.
        #  3. Both dead before either settled -> conclusively malformed.
        #  4. Otherwise keep buffering (bounded by the detection limit).
        z_done, r_done = self._z.eof, self._r.eof
        if z_done or r_done:
            best = (
                self._z
                if z_done and (not r_done or len(self._z_buf) >= len(self._r_buf))
                else self._r
            )
            return self._settle(best)
        if self._z_dead and not self._r_dead and self._r_buf:
            return self._settle(self._r)
        if self._r_dead and not self._z_dead and self._z_buf:
            return self._settle(self._z)
        if self._z_dead and self._r_dead:
            self._reject("deflate body is neither a valid zlib stream nor a raw deflate stream")
        if self._wire_seen > self._detection_buffer():
            self._reject(
                f"deflate body cannot be format-detected within {self._detection_buffer()} bytes"
            )
        return b""

    def _settle(self, best: Any) -> bytes:
        """Commit ``best``; return its decoded output so far (the bytes the
        caller has not yet emitted). Any ``unconsumed_tail`` the committed
        decoder still holds is drained by the residual loop in
        ``_accept_chunk`` — re-feeding it here would discard its output."""
        buf = self._z_buf if best is self._z else self._r_buf
        self._z_buf = self._r_buf = b""
        self._active = best
        self._committed = True
        if best is self._z:
            self._r = None
        else:
            self._z = None
        return bytes(buf)

    @property
    def residual(self) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.unconsumed_tail
        return b""  # unsettled: nothing to feed back yet

    def flush(self) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.flush()
        if self._wire_seen == 0:
            return b""  # empty body: nothing to settle
        # End-of-stream: a probe that completed cleanly wins; if both
        # probes are still mid-stream the body is truncated/malformed.
        if self._z.eof or self._r.eof:
            best = (
                self._z
                if self._z.eof and (not self._r.eof or len(self._z_buf) >= len(self._r_buf))
                else self._r
            )
            out = self._settle(best)
            return out + best.flush()
        self._reject(
            "deflate body ended before the stream completed in either zlib or raw-deflate form"
        )


class _BrotliDecoder:
    def __init__(self) -> None:
        try:
            import brotli
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ValueError("content-encoding 'br' requires the brotli package") from exc
        self._d = brotli.Decompressor()
        self._bounded = "max_length" in inspect.signature(self._d.decompress).parameters

    def decompress(self, data: bytes, max_out: int) -> bytes:
        if self._bounded:
            return self._d.decompress(data, max_length=max_out)
        return self._d.decompress(data)  # best effort; caller still caps output

    def flush(self) -> bytes:
        return b""


def _decoder_for(encoding: str, *, max_bytes: int = 0):
    """Incremental decoder for the declared Content-Encoding, or None (identity).

    Every decoder honors a per-call output cap (``decompress(data, max_out)``),
    which is what keeps decompression-bomb allocation bounded. Encodings with
    no bounded decoder available are rejected by the caller. ``max_bytes``
    seeds the deflate format-detection buffer so it is bounded by the cap.
    """
    enc = encoding.strip().lower()
    if not enc or enc == "identity":
        return None
    if enc == "gzip":
        # wbits=47 auto-detects the gzip wrapper (and plain zlib streams).
        return _ZlibDecoder(47)
    if enc == "deflate":
        return _RawDeflateDecoder(max_bytes)
    if enc == "br":
        return _BrotliDecoder()
    raise ValueError(f"content-encoding {encoding!r} cannot be decoded with a bound")


def _accept_chunk(chunk: bytes, *, decoder, out: bytearray, max_bytes: int) -> None:
    """Accept one wire chunk: decode within the window, enforce the cap.

    After a windowed ``decompress`` call the decoder may still hold
    *undecoded* input (zlib exposes it as ``unconsumed_tail``; this module
    normalizes it under ``decoder.residual``). That input must be fed back
    through bounded decoding until the decoder reports nothing more to
    give — CPython's incremental decoder does not reliably keep draining
    on empty feeds after a ``max_length``-bounded call (feeding ``b""``
    discards most of the held input and silently truncates the body).
    A call that produces no output and leaves the same input held is a
    no-progress stop (incomplete stream) — ``flush`` at end-of-stream is
    what turns that into an error.
    """
    if decoder is None:
        _check_cap(out, chunk, max_bytes)
        out += chunk
        return
    piece = b""
    feed = chunk
    while True:
        more = decoder.decompress(feed, _DECOMPRESSION_WINDOW)
        piece += more
        _check_cap(out, piece, max_bytes)
        if more:
            # Window filled: keep draining the decoder's held (undecoded)
            # input within the window.
            feed = getattr(decoder, "residual", b"") or b""
            continue
        tail = getattr(decoder, "residual", b"")
        if tail and tail != feed:
            feed = tail  # the decoder still has undecoded input — feed it back
            continue
        break
    out += piece


def _check_cap(out: bytearray, piece: bytes, max_bytes: int) -> None:
    if max_bytes > 0 and len(out) + len(piece) > max_bytes:
        raise ResponseLimitExceeded(
            f"body exceeds limit: {max_bytes} bytes (received {len(out) + len(piece)})"
        )


async def read_capped_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read an httpx response body bounded by a byte cap and an idle timeout.

    Bounds the *decoded* bytes without ever allocating an unbounded
    decompression buffer (see module docstring): identity bodies stream
    through ``aiter_raw`` with a running cap; compressed bodies are decoded
    incrementally through a windowed ``decompress(data, max_out)``, so the
    cap is enforced *during* decompression, not after it. The idle timeout
    comes from ``_limits()`` so the cap and the stall window stay in
    lockstep. On limit/stall the stream is closed, which tears down the
    underlying connection — including any SSRF proxy CONNECT tunnel.
    """
    idle = _limits()[1]
    encoding = response.headers.get("content-encoding") or ""
    try:
        decoder = _decoder_for(encoding, max_bytes=max_bytes)
    except ValueError as exc:
        # We asked for identity; a server that ignores us and compresses
        # with a codec we cannot bound must not get to allocate.
        raise ResponseLimitExceeded(
            f"response uses {encoding!r}; only identity/gzip/deflate can be "
            "decoded within the byte cap"
        ) from exc

    # Early rejection: a declared identity body already over the cap cannot
    # possibly fit, so fail before reading anything. With Content-Encoding
    # the declared size is the *compressed* size, so it is not conclusive —
    # fall through to the streaming cap (which is never bypassed).
    if max_bytes > 0 and decoder is None:
        declared = response.headers.get("content-length")
        if declared:
            with contextlib.suppress(ValueError):
                if int(declared) > max_bytes:
                    raise ResponseLimitExceeded(
                        f"declared body {declared} bytes exceeds limit {max_bytes} bytes"
                    )

    out = bytearray()
    aiter = response.aiter_raw()
    # Some transports (httpx.MockTransport, and anything that fully
    # buffers the body before handing us the response) deliver an
    # already-consumed stream: aiter_raw raises StreamConsumed and the
    # body is sitting in response.content. Take the buffered content as
    # the first chunk — the cap is still enforced before anything is
    # returned.
    preseed: list[bytes] = []
    try:
        first = await aiter.__anext__()
    except httpx.StreamConsumed:
        aiter = None
        preseed.append(response.content)
    except StopAsyncIteration:
        aiter = None
    else:
        preseed.append(first)

    while preseed:
        chunk = preseed.pop(0)
        if not chunk:
            continue
        _accept_chunk(chunk, decoder=decoder, out=out, max_bytes=max_bytes)
    while aiter is not None:
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle if idle > 0 else None)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise ResponseStalled(
                f"upstream stalled: no data for {idle}s (received {len(out)} bytes so far)"
            ) from None
        if not chunk:
            continue
        _accept_chunk(chunk, decoder=decoder, out=out, max_bytes=max_bytes)
    if decoder is not None:
        tail = decoder.flush()
        if tail:
            if max_bytes > 0 and len(out) + len(tail) > max_bytes:
                raise ResponseLimitExceeded(
                    f"body exceeds limit: {max_bytes} bytes (received {len(out) + len(tail)})"
                )
            out += tail
    return bytes(out)


async def post_json_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: dict,
    headers: dict,
    timeout: float,
) -> dict:
    """POST JSON, read the response under the byte/idle caps, return parsed JSON.

    Shared by the model and judge call paths so identical limits apply to
    both. Raises ``ResponseLimitExceeded`` / ``ResponseStalled``
    (deterministic, not retried, case not charged) or the usual httpx
    errors.
    """
    max_bytes, idle = _limits()
    # stream=True is essential: a plain ``client.post`` would already have
    # buffered the entire body before returning, defeating the cap. The
    # per-request timeout is attached to the request (httpx 0.28's
    # ``send`` does not accept a ``timeout`` kwarg). ``read=idle`` bounds
    # the stall case: no data for the idle window -> ReadTimeout.
    t = httpx.Timeout(timeout, read=idle if idle > 0 else None)
    # Ask for an uncompressed body. A compressed body decoded by httpx's
    # decoder allocates the full expanded chunk before any cap is checked;
    # with identity the wire bytes ARE the decoded bytes and the cap is
    # enforced during the read. (read_capped_body still bounds incremental
    # decompression for servers that ignore the request.)
    merged_headers = {"Accept-Encoding": "identity", **headers}
    request = client.build_request("POST", url, json=body, headers=merged_headers, timeout=t)
    response: httpx.Response | None = None
    try:
        # ``send`` blocks until headers arrive, so a ReadTimeout here means
        # the upstream (or the proxy in front of it) stalled *before*
        # sending anything — the same "dead upstream" class as a mid-body
        # stall.
        response = await client.send(request, stream=True)
        # Status first: a proxy-violation 502 carries its tag in the
        # headers and must surface as the limit exception, so the marker
        # is checked *before* the body is read (reading it would consume
        # the stream and bury the marker in the status path). Non-2xx
        # bodies are short error messages — read them under the same
        # caps and raise.
        if response.status_code >= 400:
            await read_capped_body(response, max_bytes=max_bytes)  # drain under caps
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                marker = (
                    exc.response.headers.get(_GUARD_HEADER, "") if exc.response is not None else ""
                )
                if marker == "ResponseTooLarge":
                    raise ResponseLimitExceeded(
                        "upstream response exceeds the SSRF proxy's size limit"
                    ) from exc
                if marker == "UpstreamStalled":
                    raise ResponseStalled(
                        "SSRF proxy read timed out while reading the upstream response"
                    ) from exc
                raise
            raise AssertionError("unreachable: raise_for_status did not raise")
        raw = await read_capped_body(response, max_bytes=max_bytes)
        return json.loads(raw)
    except httpx.ReadTimeout as exc:
        # A stalled upstream (no data for the read/idle window). Deterministic
        # for this call: close, fail the case, do not retry (the endpoint is
        # not going to start flowing mid-retry) and do not charge it.
        raise ResponseStalled(
            f"upstream stalled: no response data for {idle if idle > 0 else 'the read window'}s"
        ) from exc
    finally:
        # Every path — success, cap, stall, status error, parse error —
        # closes the stream, which tears down the underlying connection,
        # including any SSRF proxy CONNECT tunnel.
        if response is not None:
            await _aclose(response)
