"""Runner core: render template, call model, score, capture latency/tokens, retry."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import case, update
from sqlalchemy.orm import Session

from .db import state
from .judges import score_case
from .models import Account, Run, RunCase


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


async def execute_run(run_id: int) -> None:
    """Drive a single run to done/failed. Called by the worker."""
    session = state.session_factory()
    run = None
    try:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()

        dataset = run.dataset
        cases = (
            json.loads(dataset.cases_json)
            if dataset.cases_json
            else state.storage.get_json(dataset.storage_key)
        )
        cases = cases if isinstance(cases, list) else cases.get("cases", [])

        # Quota: the full dataset size was RESERVED at enqueue time (see
        # create_run). At settle we replace the reservation with the actual
        # charge: only cases that executed OK are billed. A failed run
        # releases its reservation entirely (refund).
        ok_cases = 0

        async with httpx.AsyncClient() as client:
            for seq, case in enumerate(cases):
                row = RunCase(run_id=run.id, seq=seq)
                try:
                    prompt = _render(run.prompt_template, case)
                    output, t_in, t_out = await _with_retries(
                        call_model,
                        client,
                        endpoint=run.endpoint,
                        model=run.model,
                        api_key=run.api_key_ref,
                        prompt=prompt,
                    )
                    judge_cfg = {
                        "endpoint": run.endpoint,
                        "model": run.model,
                        "api_key": run.api_key_ref,
                    }
                    score = await score_case(
                        client,
                        endpoint=judge_cfg["endpoint"],
                        model=judge_cfg["model"],
                        api_key=judge_cfg["api_key"],
                        output=output,
                        expected=str(case.get("expected", "")),
                        rubric=list(case.get("rubric") or []),
                    )
                    row.score = round(score.score, 4)
                    row.passed = bool(score.score >= run.threshold)
                    row.tokens_in = t_in
                    row.tokens_out = t_out
                    if state.storage is not None and run.id:
                        state.storage.put_json(
                            f"runs/{run.id}/case_{seq}/output.json",
                            {"output": output, "score": score.__dict__},
                        )
                    ok_cases += 1
                except Exception as exc:  # noqa: BLE001
                    # Execution error: case is not charged (see _settle).
                    row.error = f"{type(exc).__name__}: {exc}"
                session.add(row)
                session.flush()

        # settle: swap reservation for actual charge (only OK cases billed)
        reserved = run.total_cases
        run.status = "done"
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        cases_rows = session.query(RunCase).filter(RunCase.run_id == run.id).all()
        run.total_cases = len(cases_rows)
        passed = [r for r in cases_rows if r.passed]
        run.passed_cases = len(passed)
        scored = [r for r in cases_rows if r.score is not None]
        run.avg_score = round(sum(r.score for r in scored) / len(scored), 4) if scored else None
        _settle(session, run, charged=ok_cases, reserved=reserved, success=True)
    except Exception as exc:  # noqa: BLE001
        if run is not None:
            with contextlib.suppress(Exception):  # rollback may fail if connection is dead
                session.rollback()
            # Release the quota reservation (failed runs are free) and
            # PERSIST the terminal status — commit before the session closes.
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            _settle(
                session,
                run,
                charged=0,
                reserved=run.total_cases or 0,
                success=False,
                error=run.error,
            )
    finally:
        session.close()


def _settle(
    session: Session,
    run: Run,
    charged: int,
    reserved: int,
    success: bool,
    error: str = "",
) -> None:
    """v0 billing: the dataset size is reserved at enqueue. On success the
    reservation is swapped for the actual charge (OK cases only); on failure
    the reservation is released entirely (refund)."""
    period = time.strftime("%Y-%m")
    used = case((Account.quota_period == period, Account.used_cases), else_=0)
    remaining = case((used >= reserved, used - reserved), else_=0)
    session.execute(
        update(Account)
        .where(Account.id == run.account_id)
        .values(
            used_cases=remaining + (charged if success else 0),
            quota_period=period,
        )
        .execution_options(synchronize_session=False)
    )
    if success:
        run.total_cost_usd = 0.0  # metered by usage; cost accounting in v1
    if error:
        run.error = error
    session.commit()


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
