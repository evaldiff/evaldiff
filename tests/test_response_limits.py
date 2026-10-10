"""NEEDS-DOING A — bounded model/judge response reads.

Acceptance: integration tests cover HTTP and HTTPS, model and judge
responses, chunked and EOF-delimited bodies, compressed payloads, and
oversized headers. Valid responses at the limit succeed; excessive or
stalled responses fail without an unbounded allocation and close their
tunnel. A subsequent queued run can still complete.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from api.http_limits import (
    ResponseLimitExceeded,
    ResponseStalled,
    post_json_bounded,
)
from api.judges import rubric_llm
from api.ssrf_guard import SSRFGuard
from tests.conftest import _free_port

CAP = 2048  # test byte cap
IDLE = 0.5  # test idle timeout (s)
SMALL = 512  # body size within the cap


def _payload(n: int) -> dict:
    """An OpenAI-shaped response whose JSON body is ~n bytes."""
    return {
        "choices": [{"message": {"content": "x" * n}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _upstream_app() -> object:
    """Raw ASGI app with the pathological endpoints the matrix needs."""

    async def receive_idle(scope, receive, send):
        # Read the request body once (uvicorn delivers it whole for our
        # small bodies); do NOT loop receive() — the next message only
        # arrives on client disconnect and would block the handler forever.
        await receive()

    async def handler(scope, receive, send):
        path = scope["path"]
        # The model/judge callers POST /v1/chat/completions — serve the
        # oversized body there too (full-stack test).
        if path.endswith("/chat/completions"):
            path = "/over"
        await receive_idle(scope, receive, send)
        if path == "/ok":
            body = json.dumps(_payload(SMALL)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        elif path == "/over":
            body = json.dumps(_payload(CAP + 100)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        elif path == "/chunked-over":
            total = CAP + 100
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"transfer-encoding", b"chunked"),
                    ],
                }
            )
            chunk = json.dumps(_payload(SMALL)).encode()
            while total:
                n = min(len(chunk), total)
                await send({"type": "http.response.body", "body": chunk[:n], "more_body": True})
                total -= n
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        elif path == "/eof-over":
            # No Content-Length, no chunked: body is delimited by close.
            body = json.dumps(_payload(CAP + 100)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
        elif path == "/gzip-over":
            # Wire size small, decoded size over the cap.
            raw = json.dumps(_payload(CAP + 100)).encode()
            body = gzip.compress(raw)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-encoding", b"gzip"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        elif path == "/declared-huge":
            # Declares a 10 MiB body, sends almost nothing, then closes.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", b"10485760"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"{", "more_body": True})
            # Server closes the connection before the declared body arrives.
        elif path == "/stall":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"transfer-encoding", b"chunked"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"partial": ', "more_body": True})
            await asyncio.sleep(30)  # far longer than the idle timeout
            await send({"type": "http.response.body", "body": b'"done"}', "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-length", b"0")],
                }
            )

    return handler


def _run_server(app, host: str, port: int, ssl: bool = False, certdir: Path | None = None):
    kwargs = {}
    if ssl:
        kwargs["ssl_certfile"] = str(certdir / "cert.pem")
        kwargs["ssl_keyfile"] = str(certdir / "key.pem")

    class _ThreadingServer(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # no signals on non-main threads
            pass

    config = uvicorn.Config(app, host=host, port=port, log_level="error", **kwargs)
    server = _ThreadingServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    return server, thread


def _make_cert(tmp_path: Path) -> Path:
    d = tmp_path / "tls"
    d.mkdir(exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(d / "key.pem"),
            "-out",
            str(d / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return d


@pytest.fixture()
def upstream(tmp_path):
    """Plain-HTTP pathological upstream (127.0.0.1)."""
    server, thread = _run_server(_upstream_app(), "127.0.0.1", _free_port())
    yield f"http://127.0.0.1:{server.config.port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def https_upstream(tmp_path):
    """TLS upstream with a self-signed cert (verifiable via the bundled CA)."""
    certdir = _make_cert(tmp_path)
    server, thread = _run_server(
        _upstream_app(), "127.0.0.1", _free_port(), ssl=True, certdir=certdir
    )
    yield f"https://127.0.0.1:{server.config.port}", certdir
    server.should_exit = True
    thread.join(timeout=5)


def _client(verify: Path | None = None) -> tuple[httpx.AsyncClient, SSRFGuard]:
    """Return (client, guard); the test must stop the guard on exit."""
    guard = SSRFGuard(allow_local=True).start()
    kwargs = {"proxy": guard.proxy_url}
    if verify is not None:
        kwargs["verify"] = str(verify)
    try:
        return httpx.AsyncClient(**kwargs), guard
    except Exception:
        guard.stop()
        raise


def _set_limits(max_bytes: int, idle: float) -> None:
    from api.db import state
    from api.settings import Settings

    state.settings = Settings(
        database_url="sqlite://",
        enable_worker=False,
        rate_limit_rpm=0,
        signup_rate_per_min=0,
        response_max_bytes=max_bytes,
        response_idle_timeout=idle,
    )


async def _post(base: str, path: str, client, **kw) -> dict:
    return await post_json_bounded(
        client, base.rstrip("/") + path, body={"probe": True}, headers={}, timeout=5.0, **kw
    )


def test_http_ok_at_limit(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        data = await _post(upstream, "/ok", client)
        assert len(data["choices"][0]["message"]["content"]) == SMALL
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_http_over_cap_raises_and_client_stays_usable(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        with pytest.raises(ResponseLimitExceeded):
            await _post(upstream, "/over", client)
        # The worker (client) must still be usable afterwards.
        data = await _post(upstream, "/ok", client)
        assert data["choices"]
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_http_chunked_over_cap(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        with pytest.raises(ResponseLimitExceeded):
            await _post(upstream, "/chunked-over", client)
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_http_eof_delimited_over_cap(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        with pytest.raises(ResponseLimitExceeded):
            await _post(upstream, "/eof-over", client)
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_http_compressed_cannot_bypass_cap(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        with pytest.raises(ResponseLimitExceeded):
            await _post(upstream, "/gzip-over", client)
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_http_declared_huge_rejected_without_hang(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()
    t0 = time.monotonic()
    client2 = httpx.AsyncClient(proxy=guard.proxy_url)

    async def go():
        with pytest.raises((ResponseLimitExceeded, ResponseStalled, OSError, httpx.HTTPError)):
            await _post(upstream, "/declared-huge", client2)
        await client.aclose()
        await client2.aclose()

    asyncio.run(go())
    assert time.monotonic() - t0 < 5  # early rejection, no 10 MiB wait
    guard.stop()


def test_http_stalled_stream_times_out(upstream) -> None:
    _set_limits(CAP, IDLE)
    client, guard = _client()
    t0 = time.monotonic()

    async def go():
        with pytest.raises(ResponseStalled):
            await _post(upstream, "/stall", client)
        await client.aclose()

    asyncio.run(go())
    assert time.monotonic() - t0 < 10  # bounded by idle timeout, not the 30s server sleep
    guard.stop()


def test_https_over_cap_tunnel_path(https_upstream) -> None:
    """The CONNECT (TLS tunnel) path is bounded identically."""
    base, certdir = https_upstream
    _set_limits(CAP, IDLE)
    client, guard = _client(verify=certdir / "cert.pem")

    async def go():
        with pytest.raises(ResponseLimitExceeded):
            await _post(base, "/over", client)
        # tunnel/client still usable
        data = await _post(base, "/ok", client)
        assert data["choices"]
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_https_ok(https_upstream) -> None:
    base, certdir = https_upstream
    _set_limits(CAP, IDLE)
    client, guard = _client(verify=certdir / "cert.pem")

    async def go():
        data = await _post(base, "/ok", client)
        assert data["choices"][0]["message"]["content"]
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_judge_path_is_bounded(upstream) -> None:
    """rubric_llm reads through the same bounded reader."""
    _set_limits(CAP, IDLE)
    client, guard = _client()

    async def go():
        with pytest.raises((ResponseLimitExceeded, OSError)):
            await rubric_llm(
                client,
                endpoint=upstream,
                model="m",
                api_key="",
                output="o",
                expected="e",
                rubric=["criterion one"],
                timeout=5.0,
            )
        await client.aclose()

    asyncio.run(go())
    guard.stop()


def test_model_call_over_cap_is_recorded_and_not_retried(tmp_path) -> None:
    """Full stack: a case whose model response exceeds the cap records an
    error (deterministic, not retried), the run completes, and a subsequent
    queued run still completes (acceptance: no permanent worker loss)."""
    from starlette.testclient import TestClient

    from api.main import create_app
    from api.settings import Settings
    from tests.conftest import build_echo_judge

    server, thread = _run_server(build_echo_judge(), "127.0.0.1", _free_port())
    echo = f"http://127.0.0.1:{server.config.port}"
    huge_server, huge_thread = _run_server(_upstream_app(), "127.0.0.1", _free_port())
    huge = f"http://127.0.0.1:{huge_server.config.port}"
    try:
        app = create_app(
            Settings(
                database_url=f"sqlite:///{tmp_path}/a.db",
                enable_worker=True,
                allow_local_endpoints=True,
                rate_limit_rpm=0,
                signup_rate_per_min=0,
                response_max_bytes=CAP,
                response_idle_timeout=IDLE,
            )
        )
        with TestClient(app) as c:
            key = c.post("/v1/auth/signup", json={"email": "lim@t.com"}).json()["key"]
            H = {"Authorization": f"Bearer {key}"}
            ds = c.post(
                "/v1/datasets",
                headers=H,
                json={"name": "d", "cases": [{"input": "hi", "expected": "hi"}]},
            ).json()
            # Run against the oversized endpoint: case must error, run must finish.
            r1 = c.post(
                "/v1/runs",
                headers=H,
                json={"dataset_id": ds["id"], "model": "m", "endpoint": huge},
            ).json()
            deadline = time.time() + 20
            d1 = {}
            while time.time() < deadline:
                d1 = c.get(f"/v1/runs/{r1['id']}", headers=H).json()
                if d1["status"] in ("done", "failed"):
                    break
                time.sleep(0.2)
            assert d1["status"] in ("done", "failed"), d1
            cases = c.get(f"/v1/runs/{r1['id']}/cases", headers=H).json()
            assert cases[0]["error"] and "exceeds limit" in cases[0]["error"]
            # The worker survived: a normal run still completes.
            r2 = c.post(
                "/v1/runs",
                headers=H,
                json={"dataset_id": ds["id"], "model": "m", "endpoint": echo},
            ).json()
            deadline = time.time() + 20
            d2 = {}
            while time.time() < deadline:
                d2 = c.get(f"/v1/runs/{r2['id']}", headers=H).json()
                if d2["status"] in ("done", "failed"):
                    break
                time.sleep(0.2)
            assert d2["status"] == "done", d2
    finally:
        server.should_exit = True
        huge_server.should_exit = True
        thread.join(timeout=5)
        huge_thread.join(timeout=5)
