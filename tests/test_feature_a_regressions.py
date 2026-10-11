"""Feature A decoder regressions (second review pass).

P1  A compressed body larger than the 64 KiB decoding window must decode
    *completely*: zlib holds undecoded input in ``unconsumed_tail`` after a
    windowed call, and the loop must feed it back — feeding empty bytes
    discards it and silently truncates the body.

P2  Raw-deflate format detection must not depend on network chunk
    boundaries: a first byte arriving on its own must not commit the
    zlib-wrapped interpretation (a lone raw-deflate header byte decodes as
    a valid empty zlib stream, and committing there turns the next chunk
    into a hard error instead of a raw-deflate fallback).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import zlib

import httpx

import api.http_limits as hl
from api.db import state
from api.settings import Settings


def _set_limits(max_bytes: int, idle: float) -> None:
    state.settings = Settings(
        database_url="sqlite://",
        enable_worker=False,
        rate_limit_rpm=0,
        signup_rate_per_min=0,
        response_max_bytes=max_bytes,
        response_idle_timeout=idle,
    )


class _ChunkStream(httpx.AsyncByteStream):
    """A response body stream that yields the given chunks verbatim,
    preserving network-chunk boundaries (MockTransport + ByteStream would
    collapse everything into one chunk, masking both bugs under test)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _payload(n: int) -> bytes:
    """A JSON body of roughly n bytes."""
    pad = "x" * (n - 60)
    return json.dumps({"choices": [{"message": {"content": pad}}]}).encode()


def _post(handler, *, timeout: float = 5.0) -> dict:
    async def go() -> dict:
        return await hl.post_json_bounded(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "https://model.example/chat/completions",
            body={},
            headers={},
            timeout=timeout,
        )

    return asyncio.run(go())


def test_gzip_body_over_window_decodes_completely() -> None:
    """P1: ~200 KiB decoded from a tiny gzip stream, delivered in 8 KiB
    wire chunks — every byte must survive the 64 KiB decoding window."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(200_011)
    wire = gzip.compress(payload, compresslevel=9)
    assert len(wire) < 64 * 1024  # the interesting case: compressed < window
    chunks = [wire[i : i + 8192] for i in range(0, len(wire), 8192)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "gzip"}
        )

    data = _post(handler)
    content = data["choices"][0]["message"]["content"]
    assert len(content) == 200_011 - 60, f"truncated body: {len(content)}"
    assert content == json.loads(payload)["choices"][0]["message"]["content"]


def test_gzip_body_across_many_windows() -> None:
    """P1 (multi-window): a 300 KiB expansion crosses five decoding
    windows; the whole body must arrive intact from 64-byte wire chunks."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(300_000)
    wire = gzip.compress(payload, compresslevel=9)
    chunks = [wire[i : i + 64] for i in range(0, len(wire), 64)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "gzip"}
        )

    data = _post(handler)
    assert (
        data["choices"][0]["message"]["content"]
        == json.loads(payload)["choices"][0]["message"]["content"]
    )


def test_raw_deflate_first_byte_in_own_chunk() -> None:
    """P2: the raw-deflate header byte arrives alone; the rest follows.
    Committing the zlib-wrapped read on the empty first decode used to
    raise on the next chunk instead of falling back to raw deflate."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(200_000)
    co = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -zlib.MAX_WBITS)
    wire = co.compress(payload) + co.flush()
    chunks = [wire[:1], wire[1:]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "deflate"}
        )

    data = _post(handler)
    assert (
        data["choices"][0]["message"]["content"]
        == json.loads(payload)["choices"][0]["message"]["content"]
    )


def test_raw_deflate_arbitrary_chunking() -> None:
    """P2 (generalization): 3-byte wire chunks, format never established
    by a single chunk — the whole body must still decode."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(200_000)
    co = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -zlib.MAX_WBITS)
    wire = co.compress(payload) + co.flush()
    chunks = [wire[i : i + 3] for i in range(0, len(wire), 3)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "deflate"}
        )

    data = _post(handler)
    assert (
        data["choices"][0]["message"]["content"]
        == json.loads(payload)["choices"][0]["message"]["content"]
    )


def test_raw_deflate_over_window_decodes_completely() -> None:
    """P1 x P2: raw deflate, 300 KiB expansion (five windows), 32-byte
    chunks — both the fallback and the residual feed-back must work."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(300_000)
    co = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -zlib.MAX_WBITS)
    wire = co.compress(payload) + co.flush()
    chunks = [wire[i : i + 32] for i in range(0, len(wire), 32)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "deflate"}
        )

    data = _post(handler)
    assert (
        data["choices"][0]["message"]["content"]
        == json.loads(payload)["choices"][0]["message"]["content"]
    )


def test_zlib_wrapped_deflate_split_chunks() -> None:
    """Control: zlib-wrapped deflate with 2-byte chunks — the zlib path
    commits on real output and must not spuriously fall back to raw."""
    _set_limits(1024 * 1024, 5.0)
    payload = _payload(200_000)
    wire = zlib.compress(payload, zlib.Z_DEFAULT_COMPRESSION)
    chunks = [wire[i : i + 2] for i in range(0, len(wire), 2)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_ChunkStream(chunks), headers={"Content-Encoding": "deflate"}
        )

    data = _post(handler)
    assert (
        data["choices"][0]["message"]["content"]
        == json.loads(payload)["choices"][0]["message"]["content"]
    )


def test_bomb_still_capped_when_decoding_completes() -> None:
    """Safety control: the window fix must not weaken the cap — an
    8 MiB expansion against a 2 KiB cap still raises mid-decode."""
    _set_limits(2048, 5.0)
    wire = gzip.compress(b"p-" + b"x" * (8 * 1024 * 1024), compresslevel=9)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=httpx.ByteStream(wire), headers={"Content-Encoding": "gzip"}
        )

    with __import__("pytest").raises(hl.ResponseLimitExceeded):
        _post(handler)


def test_malformed_deflate_body_is_rejected() -> None:
    """A body that is neither valid zlib nor valid raw deflate must
    surface as a decode error (not hang, not return partial data), even
    though format settlement now waits for positive output."""
    _set_limits(1024 * 1024, 5.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkStream([b"\x01\x02\x03", b"\xfe\xff\xfd"]),
            headers={"Content-Encoding": "deflate"},
        )

    with __import__("pytest").raises(Exception) as excinfo:
        _post(handler)
    # Must be a decode failure, not a successful (truncated) parse.
    assert "choices" not in str(excinfo.value) or isinstance(
        excinfo.value, (zlib.error, json.JSONDecodeError)
    )
