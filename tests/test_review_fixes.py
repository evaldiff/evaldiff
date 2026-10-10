"""Regression coverage for response limits, comparison validity, and judge identity."""

import io
import json

import httpx
import pytest
from starlette.testclient import TestClient

from api.db import state
from api.judges import rubric_llm
from api.main import create_app
from api.models import Run
from api.settings import Settings
from api.ssrf_guard import ResponseTooLarge, _read_response


class ResponseSocket:
    def __init__(self, payload):
        self.stream = io.BytesIO(payload)

    def recv(self, size):
        return self.stream.read(size)


@pytest.mark.parametrize(
    "payload",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1000\r\n",
        b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 200,
        b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 200,
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: " + b"x" * 200,
    ],
)
def test_response_limit_rejects_all_framings(payload):
    with pytest.raises(ResponseTooLarge):
        _read_response(ResponseSocket(payload), cap=100)


@pytest.mark.parametrize(
    "payload",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nok\r\n0\r\nX: y\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n\r\nok",
    ],
)
def test_response_at_limit_is_preserved(payload):
    assert _read_response(ResponseSocket(payload), cap=len(payload)) == payload


def test_truncated_chunk_is_rejected():
    payload = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nok"
    with pytest.raises(OSError, match="incomplete"):
        _read_response(ResponseSocket(payload))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdicts, expected",
    [
        ([{"criterion": "first", "passed": True}] * 2, 0.0),
        ([{"criterion": "second", "passed": True}, {"criterion": "first", "passed": True}], 1.0),
        ([{"criterion": "first", "passed": True}], 0.5),
        ([{"criterion": "unknown", "passed": True}, {"passed": True}], 0.0),
        ([{"criterion": "first", "passed": "true"}], 0.0),
    ],
)
async def test_rubric_requires_unique_matching_verdicts(verdicts, expected):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"verdicts": verdicts})}}]},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        score = await rubric_llm(
            client,
            endpoint="https://model.example",
            model="judge",
            api_key="",
            output="answer",
            expected="answer",
            rubric=["first", "second"],
        )
    assert score.score == expected


def test_diff_and_report_reject_invalid_comparisons(tmp_path):
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/comparison.db",
            enable_worker=False,
            rate_limit_rpm=0,
            signup_rate_per_min=0,
        )
    )
    with TestClient(app) as client:
        key = client.post("/v1/auth/signup", json={"email": "review@example.com"}).json()["key"]
        headers = {"Authorization": f"Bearer {key}"}
        datasets = [
            client.post(
                "/v1/datasets",
                headers=headers,
                json={"name": name, "cases": [{"input": name, "expected": name}]},
            ).json()["id"]
            for name in ("first", "second")
        ]
        runs = [
            client.post(
                "/v1/runs",
                headers=headers,
                json={
                    "dataset_id": datasets[0],
                    "model": "test",
                    "endpoint": "https://model.example",
                },
            ).json()["id"]
            for _ in range(2)
        ]

        def check(code):
            for suffix in ("diff", "report.md"):
                response = client.get(
                    f"/v1/runs/{runs[1]}/{suffix}",
                    headers=headers,
                    params={"compare": runs[0]},
                )
                assert response.status_code == code, response.text

        for side in runs:
            for status in ("queued", "running", "failed"):
                with state.session_factory() as session:
                    for run_id in runs:
                        session.get(Run, run_id).status = status if run_id == side else "done"
                    session.commit()
                check(409)
        with state.session_factory() as session:
            for run_id in runs:
                session.get(Run, run_id).status = "done"
            session.get(Run, runs[1]).dataset_id = datasets[1]
            session.commit()
        check(409)
        with state.session_factory() as session:
            session.get(Run, runs[1]).dataset_id = datasets[0]
            session.commit()
        check(200)


def test_oversized_response_returns_502(monkeypatch):
    import socket
    import threading

    import api.ssrf_guard as guard

    original = guard._read_response
    monkeypatch.setattr(guard, "_read_response", lambda sock, **kw: original(sock, cap=100))
    client, proxy = socket.socketpair()
    upstream, server = socket.socketpair()
    try:
        monkeypatch.setattr(guard, "_dial", lambda *args, **kwargs: upstream)
        client.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        guard._handle(proxy, allow_local=False, stop=threading.Event())
        client.settimeout(1)
        response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
        assert b"200 OK" not in response
    finally:
        for sock in (client, proxy, upstream, server):
            sock.close()
