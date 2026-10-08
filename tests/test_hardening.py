"""Hardening tests: Fernet-at-rest, SSRF dial-time guard (rebinding), rate limits."""

from __future__ import annotations

import os
import socket

import pytest

# ---------- Fernet at rest (api/secrets.py) ----------


def test_fernet_roundtrip(monkeypatch) -> None:
    from api.secrets import decrypt_api_key, encrypt_api_key, generate_secret_key

    monkeypatch.setenv("EVALDIFF_SECRET_KEY", generate_secret_key())
    secret = "sk-SECRET-MODEL-KEY-123"
    stored = encrypt_api_key(secret)
    assert stored.startswith("enc:v1:")
    assert secret not in stored
    assert decrypt_api_key(stored) == secret


def test_fernet_legacy_plaintext_passes_through(monkeypatch) -> None:
    from api.secrets import decrypt_api_key

    monkeypatch.setenv("EVALDIFF_SECRET_KEY", "x" * 44)
    # rows written before the feature: untouched
    assert decrypt_api_key("sk-old-plaintext") == "sk-old-plaintext"


def test_fernet_noop_when_key_unset(monkeypatch) -> None:
    from api.secrets import encrypt_api_key

    monkeypatch.delenv("EVALDIFF_SECRET_KEY", raising=False)
    assert encrypt_api_key("sk-x") == "sk-x"


def test_fernet_wrong_key_rejected(monkeypatch) -> None:
    from api.secrets import decrypt_api_key, encrypt_api_key, generate_secret_key

    monkeypatch.setenv("EVALDIFF_SECRET_KEY", generate_secret_key())
    stored = encrypt_api_key("sk-x")
    monkeypatch.setenv("EVALDIFF_SECRET_KEY", generate_secret_key())  # different key
    with pytest.raises(RuntimeError, match="EVALDIFF_SECRET_KEY"):
        decrypt_api_key(stored)


def test_fernet_encrypted_but_no_key_in_env(monkeypatch) -> None:
    from api.secrets import decrypt_api_key, encrypt_api_key, generate_secret_key

    monkeypatch.setenv("EVALDIFF_SECRET_KEY", generate_secret_key())
    stored = encrypt_api_key("sk-x")
    monkeypatch.delenv("EVALDIFF_SECRET_KEY")
    with pytest.raises(RuntimeError, match="not set"):
        decrypt_api_key(stored)


def test_api_key_encrypted_at_rest(client, tmp_path) -> None:
    """End-to-end: a run submitted with a model key stores enc:v1:<...>."""
    import sqlite3

    import api.secrets as sekm

    os.environ["EVALDIFF_SECRET_KEY"] = sekm.generate_secret_key()
    try:
        key = client.post("/v1/auth/signup", json={"email": "enc@example.com"}).json()["key"]
        H = {"Authorization": f"Bearer {key}"}
        ds = client.post(
            "/v1/datasets",
            headers=H,
            json={"name": "d", "cases": [{"input": "i", "expected": "e"}]},
        ).json()
        r = client.post(
            "/v1/runs",
            headers=H,
            json={
                "dataset_id": ds["id"],
                "model": "m",
                "endpoint": "http://127.0.0.1:9/v1",  # never reached: fails at dial time
                "api_key": "sk-VISIBLE-PLAINTEXT",
            },
        )
        assert r.status_code == 202
        row = (
            sqlite3.connect(f"{tmp_path}/test.db")
            .execute("select api_key_ref from runs order by id desc limit 1")
            .fetchone()
        )
        assert row[0].startswith("enc:v1:")
        assert "sk-VISIBLE-PLAINTEXT" not in row[0]
    finally:
        del os.environ["EVALDIFF_SECRET_KEY"]


# ---------- SSRF guard: dial-time validation (DNS rebinding window) ----------


def test_guard_blocks_literal_private_at_dial() -> None:
    import httpx

    from api.ssrf_guard import SSRFGuard

    guard = SSRFGuard(allow_local=False).start()
    try:
        with httpx.Client(proxy=guard.proxy_url, timeout=10) as c:
            for evil in ("http://127.0.0.1:9/x", "http://169.254.169.254/m", "http://10.0.0.1/x"):
                r = c.get(evil)
                # httpx returns the 403 as a normal response — assert the guard
                # actually rejected it (not "allowed" and not a dial failure).
                assert r.status_code == 403, f"{evil} -> {r.status_code}"
                assert "blocked" in r.text
    finally:
        guard.stop()


def test_guard_allows_public_endpoints() -> None:
    import httpx

    from api.ssrf_guard import SSRFGuard

    guard = SSRFGuard(allow_local=False).start()
    try:
        with httpx.Client(proxy=guard.proxy_url, timeout=20) as c:
            r = c.get("http://example.com/")
            assert r.status_code == 200
    finally:
        guard.stop()


def test_guard_rebinding_window_closed(monkeypatch) -> None:
    """A hostname that resolves fine at submit time but rebinds to a private
    address at dial time must be REFUSED — the pre-flight check cannot see
    this, the dial-time check must."""
    import httpx

    import api.ssrf_guard as sg

    original = socket.getaddrinfo

    def rebinding(*args, **kwargs):
        host = args[0]
        if host == "rebind.example":
            # first (submit-time) lookups see a safe answer; the guard's
            # dial-time lookup returns loopback — simulating a rebinding TTL.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        return original(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    guard = sg.SSRFGuard(allow_local=False).start()
    try:
        with httpx.Client(proxy=guard.proxy_url, timeout=10) as c:
            r = c.get("http://rebind.example/x")
            # get() does not raise on 4xx — assert the guard rejected the
            # dial-time-rebound address (loopback), not that the call "worked".
            assert r.status_code == 403, f"rebinding allowed: {r.status_code} {r.text}"
            assert "dial time" in r.text
    finally:
        guard.stop()


def test_guard_allow_local_permits_loopback(monkeypatch) -> None:
    """Deployment opt-in (self-hosted model servers) still works through the
    guard, and only for the opted-in deployment."""
    import httpx

    from api.ssrf_guard import SSRFGuard

    guard = SSRFGuard(allow_local=True).start()
    try:
        with httpx.Client(proxy=guard.proxy_url, timeout=5) as c:
            # dial must be ATTEMPTED (not policy-blocked): connection refused
            # by the OS, or a real response — never 403 from the guard.
            try:
                c.get("http://127.0.0.1:9/x")
            except httpx.HTTPStatusError as e:
                assert e.response.status_code != 403
            except httpx.HTTPError:
                pass  # refused by OS: expected (nothing listens on :9)
    finally:
        guard.stop()


# ---------- Rate limiting ----------


def _app_with_limits(
    tmp_path, *, rpm: float, burst: int, signup_rpm: float = 5, signup_burst: int = 3
):
    from api.main import create_app
    from api.settings import Settings

    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/rl.db",
            enable_worker=False,
            rate_limit_rpm=rpm,
            rate_limit_burst=burst,
            signup_rate_per_min=signup_rpm,
            signup_burst=signup_burst,
        )
    )


def test_account_rate_limit_429_with_retry_after(tmp_path) -> None:
    from starlette.testclient import TestClient

    app = _app_with_limits(tmp_path, rpm=0.5, burst=2)
    with TestClient(app) as c:
        key = c.post("/v1/auth/signup", json={"email": "rl@example.com"}).json()["key"]
        H = {"Authorization": f"Bearer {key}"}
        results = []
        for _ in range(6):
            r = c.get("/v1/usage", headers=H)
            results.append(r)
        codes = [r.status_code for r in results]
        assert codes.count(200) == 2, f"expected exactly the burst of 2, got {codes}"
        assert all(r.status_code == 429 for r in results[2:])
        assert int(results[2].headers.get("retry-after", "0")) >= 1


def test_signup_rate_limit_is_separate_and_stricter(tmp_path) -> None:
    from starlette.testclient import TestClient

    app = _app_with_limits(tmp_path, rpm=10, burst=10, signup_rpm=1, signup_burst=2)
    with TestClient(app) as c:
        r1 = c.post("/v1/auth/signup", json={"email": "a1@example.com"})
        r2 = c.post("/v1/auth/signup", json={"email": "a2@example.com"})
        r3 = c.post("/v1/auth/signup", json={"email": "a3@example.com"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r3.status_code == 429
        assert "retry-after" in {k.lower() for k in r3.headers}


def test_limiter_disabled_by_zero_settings(tmp_path) -> None:
    from starlette.testclient import TestClient

    app = _app_with_limits(tmp_path, rpm=0, burst=0, signup_rpm=0, signup_burst=0)
    with TestClient(app) as c:
        key = c.post("/v1/auth/signup", json={"email": "noflimit@example.com"}).json()["key"]
        for i in range(30):
            assert c.get("/v1/usage", headers={"Authorization": f"Bearer {key}"}).status_code == 200
