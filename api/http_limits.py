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
from typing import Any

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
    not depend on where the network chunk boundaries fall. So while the
    format is unsettled we **accumulate** the wire input and, on each call,
    re-try both interpretations on the whole accumulated buffer:

    - zlib-wrapped first (``wbits=15``);
    - then raw deflate (``wbits=-15``).

    Whichever interpretation actually *yields output* wins and is
    committed; from then on the committed stateful decoder is used
    directly and ``residual`` (its ``unconsumed_tail``) drives the window
    loop in ``_accept_chunk``.

    Crucially, a ``zlib.error`` on a prefix does **not** commit the other
    format: a mid-block prefix of a *valid* zlib-wrapped stream can raise
    ("invalid stored block lengths") while a longer prefix decodes fine.
    We only ever commit on positive output, so the format is established
    by evidence, not by a failed guess. A genuinely malformed stream is
    reported by ``flush`` at end-of-stream, once the whole body has
    accumulated and both interpretations have had their chance.
    """

    def __init__(self) -> None:
        self._committed = False
        self._active: Any = None  # a zlib.decompressobj once committed
        self._acc = b""  # wire input held while the format is unsettled

    def decompress(self, data: bytes, max_out: int) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.decompress(data, max_out)
        self._acc += data
        # Settle the format on the accumulated buffer: the first
        # interpretation that yields output wins.
        for wbits in (15, -zlib.MAX_WBITS):
            try:
                d = zlib.decompressobj(wbits)
                out = d.decompress(self._acc, max_out)
            except zlib.error:
                continue
            if out:
                self._active = d
                self._committed = True
                self._acc = b""
                return out
        # Incomplete stream — neither interpretation produced output yet.
        # Keep the buffer; wait for more wire data.
        return b""

    @property
    def residual(self) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.unconsumed_tail
        return b""  # accumulating: nothing to feed back yet

    def flush(self) -> bytes:
        if self._committed:
            assert self._active is not None
            return self._active.flush()
        if not self._acc:
            return b""  # empty body: nothing to settle
        # End-of-stream arrived before the format settled: give both
        # interpretations a final shot at the complete buffer.
        for wbits in (15, -zlib.MAX_WBITS):
            try:
                d = zlib.decompressobj(wbits)
                out = d.decompress(self._acc)
            except zlib.error:
                continue
            self._active = d
            self._committed = True
            self._acc = b""
            return out + d.flush()
        # Neither format could decode the body — malformed.
        raise zlib.error("deflate body could not be decoded as zlib or raw deflate")


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


def _decoder_for(encoding: str):
    """Incremental decoder for the declared Content-Encoding, or None (identity).

    Every decoder honors a per-call output cap (``decompress(data, max_out)``),
    which is what keeps decompression-bomb allocation bounded. Encodings with
    no bounded decoder available are rejected by the caller.
    """
    enc = encoding.strip().lower()
    if not enc or enc == "identity":
        return None
    if enc == "gzip":
        # wbits=47 auto-detects the gzip wrapper (and plain zlib streams).
        return _ZlibDecoder(47)
    if enc == "deflate":
        return _RawDeflateDecoder()
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
        decoder = _decoder_for(encoding)
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
