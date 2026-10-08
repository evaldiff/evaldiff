"""Runner core: render template, call model (through the SSRF guard), score, capture latency/tokens, retry.

Transaction discipline (P1 fix)
-------------------------------
Every database write in this module is flushed AND committed immediately.
The v0 code flushed the first case row and held that write transaction
open across the ENTIRE run (model calls can take minutes), so a single
active run blocked all other SQLite writers (signups, dataset creation,
run submissions) with "database is locked". Short transactions remove
that: the write lock is held only for the milliseconds around each commit.

Quota (P2 fix): settlement goes through the reservation ledger in
``api.quota`` — idempotent, and netted against the period the reservation
was made in, not the current month.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from .db import state
from .judges import score_case
from .leases import LeaseLost, fence
from .models import Run, RunCase
from .quota import settle_run
from .secrets import decrypt_api_key
from .ssrf_guard import SSRFGuard


async def call_model(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    timeout: float = 120.0,
) -> tuple[str, int, int]:
    """OpenAI-compatible chat completion. Returns (text, tokens_in, tokens_out)."""
    url = endpoint.rstrip("/")
    if url.endswith("/chat/completions"):
        pass
    elif url.endswith("/v1"):
        url = url + "/chat/completions"
    else:
        url = url + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = await client.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    return text, tokens_in, tokens_out


async def _execute_cases(run: Run, session: Session, cases: list, protect, attempt: str) -> int:
    """Run every case through model + judge. Returns the number of OK cases.

    Each case row is flushed and committed immediately: the write
    transaction around a case write is milliseconds, not the whole run —
    concurrent API writers are not blocked while models are called.
    """
    ok_cases = 0
    # All model traffic goes through the in-process SSRF guard: the target
    # is re-resolved and re-validated at dial time, so a hostname that
    # rebinds to a private address between the submit-time check and the
    # connection is still refused. Bound to 127.0.0.1, ephemeral port.
    allow_local = bool(state.settings.allow_local_endpoints) if state.settings else False
    guard = SSRFGuard(allow_local=allow_local).start()
    try:
        # Decrypt the model key once per run (Fernet at rest, plaintext in
        # memory only for the duration of this run).
        api_key = decrypt_api_key(run.api_key_ref)
        async with httpx.AsyncClient(proxy=guard.proxy_url) as client:
            for seq, c in enumerate(cases):
                row = RunCase(run_id=run.id, seq=seq)
                try:
                    prompt = _render(run.prompt_template, c)
                    output, t_in, t_out = await _with_retries(
                        call_model,
                        client,
                        endpoint=run.endpoint,
                        model=run.model,
                        api_key=api_key,
                        prompt=prompt,
                    )
                    score = await score_case(
                        client,
                        endpoint=run.endpoint,
                        model=run.model,
                        api_key=api_key,
                        output=output,
                        expected=str(c.get("expected", "")),
                        rubric=list(c.get("rubric") or []),
                    )
                    row.score = round(score.score, 4)
                    row.passed = bool(score.score >= run.threshold)
                    row.tokens_in = t_in
                    row.tokens_out = t_out
                    if state.storage is not None and run.id:
                        row.output_key = state.storage.put_json(
                            f"runs/{run.id}/attempts/{attempt}/case_{seq}/output.json",
                            {"output": output, "score": score.__dict__},
                        )
                    ok_cases += 1
                except Exception as exc:  # noqa: BLE001
                    # Execution error: case is not charged (see settle_run).
                    row.error = f"{type(exc).__name__}: {exc}"
                protect(session)
                session.add(row)
                session.commit()  # short transaction: release the write lock now
    finally:
        guard.stop()
    return ok_cases


async def execute_run(
    run_id: int, *, job_id: int | None = None, lease_token: str | None = None
) -> None:
    """Drive a single run to done/failed. Called by the worker."""
    session = state.session_factory()
    run = None

    def protect(session):
        if job_id is not None:
            fence(session, job_id, lease_token)

    try:
        protect(session)
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()  # release before any model I/O

        dataset = run.dataset
        cases = (
            json.loads(dataset.cases_json)
            if dataset.cases_json
            else state.storage.get_json(dataset.storage_key)
        )
        cases = cases if isinstance(cases, list) else cases.get("cases", [])

        # Quota: the full dataset size was RESERVED at enqueue time (ledger
        # row in api.quota). At settle we record the actual charge (OK
        # cases only); a failed run's reservation is fully refunded. The
        # ledger settles against the reservation's own period and is
        # idempotent — a re-settlement after a crash is a no-op.
        ok_cases = await _execute_cases(run, session, cases, protect, lease_token or uuid4().hex)

        protect(session)
        run.status = "done"
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        cases_rows = session.query(RunCase).filter(RunCase.run_id == run.id).all()
        run.total_cases = len(cases_rows)
        passed = [r for r in cases_rows if r.passed]
        run.passed_cases = len(passed)
        scored = [r for r in cases_rows if r.score is not None]
        run.avg_score = round(sum(r.score for r in scored) / len(scored), 4) if scored else None
        run.total_cost_usd = 0.0  # metered by usage; cost accounting in v1
        settle_run(session, run_id=run.id, charged=ok_cases, success=True)
        session.commit()
    except LeaseLost:
        session.rollback()
    except Exception as exc:  # noqa: BLE001
        if run is not None:
            with contextlib.suppress(Exception):  # rollback may fail if connection is dead
                session.rollback()
            try:
                protect(session)
            except LeaseLost:
                return
            # Refund the reservation (idempotent) and PERSIST the terminal
            # status — commit before the session closes.
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            settle_run(session, run_id=run.id, charged=0, success=False)
            session.commit()
    finally:
        session.close()


async def _with_retries(fn, *args, attempts: int = 3, base_delay: float = 0.5, **kwargs):
    last = None
    for i in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2**i))
    raise last


def _render(template: str, case: dict) -> str:
    """Tiny {{ var }} renderer — no Jinja dependency for v0."""
    out = template
    for key, value in case.items():
        if key in ("input", "expected", "rubric", "tags"):
            out = out.replace("{{ " + key + " }}", str(value))
            out = out.replace("{{" + key + "}}", str(value))
    return out
