"""Shared bounded JSON POST for model and judge calls (NEEDS-DOING A).

Closes the last unbounded path left after the SSRF guard: an upstream that
returns (or dribbles) an unbounded response body. Both the model path
(``api.runner.call_model``) and the judge path (``api.judges.rubric_llm``)
read their response through ``post_json_bounded``, so the limits apply to
both, over plain HTTP and over HTTPS (the SSRF-guard CONNECT tunnel):

- **byte cap** on the *decoded* body (httpx decodes Content-Encoding before
  yielding chunks, so compression cannot bypass the cap),
- **idle timeout** between data chunks — a slow-drip upstream under the cap
  must not pin a worker,
- **stream close** on cap/stall — tearing the response body down also tears
  the proxy tunnel down, so no long-lived connection survives a violation.

The proxy's own plain-HTTP cap remains as an additional bound. Limits are
configurable: ``EVALDIFF_RESPONSE_MAX_BYTES``,
``EVALDIFF_RESPONSE_IDLE_TIMEOUT``. A cap of 0 disables the byte limit.

``ResponseLimitExceeded`` / ``ResponseStalled`` are deliberately NOT
``httpx.TransportError``: ``_with_retries`` must not retry them — a
deterministic size violation or a dead upstream is not going to get better
on retry, and the failed case is not charged (``_execute_cases`` records
the error and skips billing).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx

DEFAULT_RESPONSE_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB decoded body
DEFAULT_RESPONSE_IDLE_TIMEOUT = 10.0  # seconds of no data => stalled


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


async def read_capped_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read an httpx response body bounded by a byte cap and an idle timeout.

    Bounds the *decoded* bytes (``aiter_bytes`` yields post-decode chunks),
    so Content-Encoding cannot bypass the cap. The idle timeout comes from
    ``_limits()`` so the cap and the stall window stay in lockstep. On
    limit/stall the stream is closed, which tears down the underlying
    connection — including any SSRF proxy CONNECT tunnel.
    """
    idle = _limits()[1]
    # Early rejection: a declared identity body already over the cap cannot
    # possibly fit, so fail before reading anything. With Content-Encoding
    # the declared size is the *compressed* size, so it is not conclusive —
    # fall through to the streaming cap (which is never bypassed).
    if max_bytes > 0 and "content-encoding" not in response.headers:
        declared = response.headers.get("content-length")
        if declared:
            with contextlib.suppress(ValueError):
                if int(declared) > max_bytes:
                    await _aclose(response)
                    raise ResponseLimitExceeded(
                        f"declared body {declared} bytes exceeds limit {max_bytes} bytes"
                    )
    out = bytearray()
    aiter = response.aiter_bytes()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(), timeout=idle if idle > 0 else None
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise ResponseStalled(
                    f"upstream stalled: no data for {idle}s (received {len(out)} bytes so far)"
                ) from None
            if not chunk:
                continue
            if max_bytes > 0 and len(out) + len(chunk) > max_bytes:
                raise ResponseLimitExceeded(
                    f"body exceeds limit: {max_bytes} bytes (received {len(out) + len(chunk)})"
                )
            out += chunk
    except (ResponseLimitExceeded, ResponseStalled):
        # Close promptly: releases the pooled connection and, for the
        # proxy path, tears the CONNECT tunnel down.
        await _aclose(response)
        raise
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
    both. Raises ``ResponseLimitExceeded`` / ``ResponseStalled`` (deterministic,
    not retried, case not charged) or the usual httpx errors.
    """
    max_bytes, idle = _limits()
    # stream=True is essential: a plain ``client.post`` would already have
    # buffered the entire body before returning, defeating the cap. The
    # per-request timeout is attached to the request (httpx 0.28's
    # ``send`` does not accept a ``timeout`` kwarg). ``read=idle`` bounds
    # the stall case: no data for the idle window -> ReadTimeout.
    t = httpx.Timeout(timeout, read=idle if idle > 0 else None)
    request = client.build_request("POST", url, json=body, headers=headers, timeout=t)
    response: httpx.Response | None = None
    try:
        # ``send`` blocks until headers arrive, so a ReadTimeout here means
        # the upstream (or the proxy in front of it) stalled *before*
        # sending anything — the same "dead upstream" class as a mid-body
        # stall.
        response = await client.send(request, stream=True)
        raw = await read_capped_body(response, max_bytes=max_bytes)
        response.raise_for_status()
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
