"""Shared fixtures: temp DB, app client, and a fake echo model endpoint."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from starlette.testclient import TestClient

from api.db import state
from api.main import create_app
from api.settings import Settings


def build_echo_judge() -> FastAPI:
    """OpenAI-compatible endpoint: echoes the prompt; returns rubric verdicts when judging."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    def chat(payload: dict):
        prompt = payload["messages"][0]["content"]
        if "CRITERIA:" in prompt:
            criteria = re.findall(r"- (.+)", prompt.split("CRITERIA:")[1])
            verdicts = [
                {
                    "criterion": c,
                    "passed": any(k in prompt for k in ("30-day", "full refund")),
                }
                for c in criteria
            ]
            return {
                "choices": [{"message": {"content": json.dumps({"verdicts": verdicts})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        text = f"echo: {prompt}"
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {
                "prompt_tokens": max(1, len(prompt) // 4),
                "completion_tokens": max(1, len(text) // 4),
            },
        }

    return app


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture()
def echo_model():
    """Run the fake model endpoint on a local port for the test duration."""
    port = _free_port()
    config = uvicorn.Config(build_echo_judge(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "echo model server failed to start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def api_database_url(tmp_path):
    """Use a unique PostgreSQL schema in the PG job; retain SQLite in the default job."""
    configured = os.environ.get("EVALDIFF_TEST_POSTGRES_URL")
    if not configured:
        yield f"sqlite:///{tmp_path}/test.db"
        return
    url = make_url(configured)
    if url.get_backend_name() != "postgresql":
        raise ValueError("EVALDIFF_TEST_POSTGRES_URL must reference PostgreSQL")
    schema = "api_" + uuid4().hex
    admin = create_engine(url, hide_parameters=True)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def client(api_database_url, echo_model):
    settings = Settings(
        database_url=api_database_url,
        enable_worker=True,
        allow_local_endpoints=True,  # tests use a local echo model (127.0.0.1)
        rate_limit_rpm=0,  # off by default in tests (dedicated tests enable it)
        signup_rate_per_min=0,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    # reset global state between tests
    state.session_factory = None
    state.storage = None
