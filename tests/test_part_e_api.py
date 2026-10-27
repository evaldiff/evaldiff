"""Part E — API endpoints behind the CLI gate + LocalStorage.

Covers the evidence endpoints the CLI report builds on:
  GET /v1/datasets/{id}/cases          — input/expected per case
  GET /v1/runs/{id}/cases/{seq}/output — actual output + judge verdict
and the LocalStorage implementation (atomic, bounded, traversal-safe).
"""

from __future__ import annotations

import pytest
from test_api import _wait_for

from api.settings import Settings
from api.storage import InMemoryStorage, LocalStorage, build_storage


def _signup(client, email: str) -> tuple[str, dict]:
    key = client.post("/v1/auth/signup", json={"email": email}).json()["key"]
    return key, {"Authorization": f"Bearer {key}"}


def test_dataset_cases_endpoint(client) -> None:
    _key, headers = _signup(client, "parte-ds@example.com")
    key2, _ = _signup(client, "parte-other@example.com")
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "part-e",
            "cases": [
                {"input": "in0", "expected": "exp0", "rubric": ["mentions 30-day"]},
                {"input": "in1", "expected": "exp1"},
            ],
        },
    ).json()
    r = client.get(f"/v1/datasets/{ds['id']}/cases", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_count"] == 2
    assert body["cases"][0]["input"] == "in0"
    assert body["cases"][0]["rubric"] == ["mentions 30-day"]
    # seq = 0-based dataset position — the join key the CLI report builders
    # rely on (user case JSON carries no seq of its own).
    assert [c["seq"] for c in body["cases"]] == [0, 1]
    # account-scoped: other account gets 404 (no existence leakage)
    assert (
        client.get(
            f"/v1/datasets/{ds['id']}/cases", headers={"Authorization": f"Bearer {key2}"}
        ).status_code
        == 404
    )
    assert client.get("/v1/datasets/9999/cases", headers=headers).status_code == 404


def test_case_output_evidence(client, echo_model) -> None:
    key, headers = _signup(client, "parte-out@example.com")
    key2, _ = _signup(client, "parte-out2@example.com")
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "part-e-out",
            "cases": [
                {"input": "What is your refund policy?", "expected": "30-day full refund"},
            ],
        },
    ).json()
    r = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "echo-1", "endpoint": echo_model, "threshold": 0.0},
    )
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    out = _wait_for(client, key, f"/v1/runs/{rid}", "done")
    assert out["status"] == "done"

    r = client.get(f"/v1/runs/{rid}/cases/0/output", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seq"] == 0
    assert body["output"]  # actual model output is present
    assert body["score"]["score"] is not None
    # unknown seq / other account
    assert client.get(f"/v1/runs/{rid}/cases/99/output", headers=headers).status_code == 404
    assert (
        client.get(
            f"/v1/runs/{rid}/cases/0/output", headers={"Authorization": f"Bearer {key2}"}
        ).status_code
        == 404
    )


# ---------- LocalStorage ----------
def test_local_storage_roundtrip(tmp_path) -> None:
    st = LocalStorage(str(tmp_path / "store"))
    st.put_json("runs/1/attempts/1/case_0/output.json", {"output": "hello", "n": 1})
    assert st.get_json("runs/1/attempts/1/case_0/output.json") == {"output": "hello", "n": 1}


def test_local_storage_traversal_blocked(tmp_path) -> None:
    st = LocalStorage(str(tmp_path / "store"))
    for bad in ("../escape.json", "/abs.json", "a\x00b", ""):
        with pytest.raises((ValueError, KeyError)):
            st.put_json(bad, {"x": 1})
        with pytest.raises((ValueError, KeyError)):
            st.get_json(bad)
    assert not (tmp_path / "escape.json").exists()


def test_local_storage_size_bound(tmp_path) -> None:
    st = LocalStorage(str(tmp_path / "store"), max_bytes=64)
    with pytest.raises(ValueError):
        st.put_json("big.json", {"x": "y" * 128})
    st.put_json("small.json", {"x": 1})
    assert st.get_json("small.json") == {"x": 1}


def test_local_storage_missing_key(tmp_path) -> None:
    st = LocalStorage(str(tmp_path / "store"))
    with pytest.raises(KeyError):
        st.get_json("nope.json")


def test_build_storage_wiring() -> None:
    assert isinstance(build_storage(Settings()), InMemoryStorage)
    assert isinstance(build_storage(Settings(storage_dir="/tmp/evaldiff-store")), LocalStorage)


def test_storage_dir_settings_default_none() -> None:
    s = Settings()
    assert s.storage_dir is None
