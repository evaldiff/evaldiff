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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from .db import state
from .http_limits import post_json_bounded
from .judges import Score, score_case
from .leases import LeaseLost, fence
from .models import Run, RunCase
from .quota import remaining_deadline_seconds, settle_run
from .secrets import decrypt_api_key
from .ssrf_guard import SSRFGuard


class RunDeadlineExceeded(Exception):
    """The run's persisted wall-clock budget is spent (NEEDS-DOING C)."""


_pool: ThreadPoolExecutor | None = None
_pool_size = 4
_pool_sem: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
_pool_lock = threading.Lock()


def _storage_pool(
    loop: asyncio.AbstractEventLoop,
) -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
    """Bounded pool + per-loop admission semaphore for synchronous storage
    calls (NEEDS-DOING C).

    A stalled S3 call occupies a thread until the client's own transport
    timeout and cannot be cancelled. Three bounds keep that from leaking:

    - the pool caps LIVE threads (``max_storage_threads``);
    - the admission semaphore is acquired BEFORE ``pool.submit``, so a
      call whose waiter times out while waiting for a slot never enters
      the executor's UNBOUNDED internal queue — the old
      submit-then-wait pattern let 100 timed-out submissions queue 100
      work items behind one blocked thread, growing memory without
      limit;
    - the underlying boto3 calls have a finite lifetime (connect/read
      timeouts, ``storage_timeout``), so a stuck call dies on its own,
      freeing its thread and slot instead of pinning them — and
      delaying process shutdown — for the life of the process.

    The semaphore is released only when the work ACTUALLY finishes (a
    done-callback on the concurrent.futures.Future, hopped onto the loop
    thread via ``call_soon_threadsafe``) — NOT when the waiter times
    out. A timed-out waiter that released its slot early would admit a
    replacement call right back into the stalled pool's queue, which is
    exactly the leak being closed.

    The pool (a plain threading executor) is loop-independent and shared;
    the semaphore is loop-bound (asyncio 3.10+) and therefore created
    PER loop — production runs one loop, tests run one per ``asyncio.run``.
    """
    global _pool, _pool_size
    with _pool_lock:
        if _pool is None:
            settings = state.settings
            if settings is None:
                from .settings import Settings

                settings = Settings()
            _pool_size = max(1, int(getattr(settings, "max_storage_threads", 4)))
            _pool = ThreadPoolExecutor(
                max_workers=_pool_size, thread_name_prefix="evaldiff-storage"
            )
        sem = _pool_sem.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(_pool_size)
            _pool_sem[loop] = sem
        return _pool, sem


async def _storage_call(fn, *args) -> object:
    """One storage call: admitted, off the event loop, on the bounded pool.

    Admission happens before the work item is enqueued; the slot is
    returned only when the work completes (see ``_storage_pool``).
    Callers that time out while waiting for a slot raise (or are
    cancelled) without ever touching the executor queue.
    """
    loop = asyncio.get_running_loop()
    _pool, sem = _storage_pool(loop)
    await sem.acquire()
    try:
        cfut = _pool.submit(fn, *args)
    except BaseException:
        sem.release()  # submit failed: do not leak the admission slot
        raise

    def _release(_done: object) -> None:
        # Fires in the worker thread when the call REALLY finishes (or
        # errors). Hopped onto the loop thread; suppressed when the loop
        # is already closed (shutdown) — the pool is being torn down.
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(sem.release)

    cfut.add_done_callback(_release)
    return await asyncio.wrap_future(cfut)


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
    # Bounded read: byte cap + idle timeout on the decoded body (NEEDS-DOING
    # A). Deterministic violations raise ResponseLimitExceeded /
    # ResponseStalled — NOT httpx.TransportError, so _with_retries won't
    # retry them, and the case is not charged.
    data = await post_json_bounded(client, url, body=body, headers=headers, timeout=timeout)
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    return text, tokens_in, tokens_out


async def _execute_case(
    run: Run,
    client: httpx.AsyncClient,
    case: dict,
    api_key: str,
    *,
    attempt: str,
    seq: int,
    remaining: int | None,
) -> tuple[str, int, int, Score, str | None]:
    """One case: render, model call (with retries), judge, persist result.
    Returns (output, tokens_in, tokens_out, score, output_key).

    The model call and judge run under the caller's deadline guard (they
    are cancelled in flight on expiry). The result write to storage runs
    off the event loop (bounded pool + admission semaphore) so a slow
    backend cannot stall lease heartbeats; the deadline bounds the wait,
    and a write that lands after the deadline does not charge the case —
    it raises RunDeadlineExceeded instead."""
    prompt = _render(run.prompt_template, case)
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
        expected=str(case.get("expected", "")),
        rubric=list(case.get("rubric") or []),
    )
    output_key = None
    if state.storage is not None and run.id:
        # Synchronous storage client, off the event loop (P1) and on the
        # bounded pool (P2): the pool caps live threads, the admission
        # semaphore caps queued work items (timed-out waiters never enter
        # the executor queue), and the client's transport timeout gives
        # the in-flight call a finite lifetime. A late write is
        # DISCARDED — it must not charge the case. The event loop is
        # never blocked by the write, so heartbeats and concurrent
        # attempts keep running.
        key = f"runs/{run.id}/attempts/{attempt}/case_{seq}/output.json"
        payload = {"output": output, "score": score.__dict__}
        if remaining is not None and remaining > 0:
            try:
                output_key = await asyncio.wait_for(
                    _storage_call(state.storage.put_json, key, payload),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                # Deadline lapsed while the write was in flight (or
                # waiting for a slot): the in-flight write may still
                # complete in its thread, but it is DISCARDED — the case
                # is not charged.
                raise RunDeadlineExceeded("run deadline exceeded") from None
        else:
            # Unlimited budget (or none left, caught by the caller): still
            # off the event loop.
            output_key = await _storage_call(state.storage.put_json, key, payload)
    return output, t_in, t_out, score, output_key


async def _execute_cases(run: Run, session: Session, cases: list, protect, attempt: str) -> int:
    """Run every case through model + judge. Returns the number of OK cases.

    Each case row is flushed and committed immediately: the write
    transaction around a case write is milliseconds, not the whole run —
    concurrent API writers are not blocked while models are called.

    Deadline enforcement (NEEDS-DOING C): the run's absolute deadline
    bounds EVERY part of a case — model call, retries, judge, storage.
    When the deadline lapses mid-case, ``asyncio.wait_for`` cancels the
    in-flight work; the case is NOT charged and the run is settled
    terminally by ``execute_run`` (raised as RunDeadlineExceeded).
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
        api_key = decrypt_api_key(run.api_key_ref, settings=state.settings)
        async with httpx.AsyncClient(proxy=guard.proxy_url) as client:
            for seq, c in enumerate(cases):
                # Run-wide deadline (NEEDS-DOING C): the persisted budget is
                # absolute, so a reclaimed attempt inherits what was left.
                # Refuse to start a new case when there is no meaningful time
                # left (one case involves a model call + judge call).
                _check_deadline(run)
                remaining = remaining_deadline_seconds(run)
                row = RunCase(run_id=run.id, seq=seq)
                try:
                    if remaining is not None:
                        # The deadline covers the WHOLE case — model call,
                        # retries, judge, and the result write to storage.
                        # wait_for cancels in-flight work on expiry; the
                        # storage write runs on the bounded pool (off the
                        # event loop) and is charged only if it lands
                        # before the deadline (P1 + P2 fixes).
                        _output, t_in, t_out, score, output_key = await asyncio.wait_for(
                            _execute_case(
                                run,
                                client,
                                c,
                                api_key,
                                attempt=attempt,
                                seq=seq,
                                remaining=remaining,
                            ),
                            timeout=remaining,
                        )
                    else:
                        _output, t_in, t_out, score, output_key = await _execute_case(
                            run, client, c, api_key, attempt=attempt, seq=seq, remaining=remaining
                        )
                    row.score = round(score.score, 4)
                    row.passed = bool(score.score >= run.threshold)
                    row.tokens_in = t_in
                    row.tokens_out = t_out
                    row.output_key = output_key
                    ok_cases += 1
                except asyncio.TimeoutError:
                    # Deadline lapsed mid-case: the in-flight model/retry/
                    # judge work is already cancelled. The case is not
                    # charged; the run is settled terminally in execute_run.
                    raise RunDeadlineExceeded("run deadline exceeded") from None
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

        # The deadline covers the dataset read too (NEEDS-DOING C): a
        # stalled S3 read must fail the run — settling quota and freeing
        # the worker slot — instead of holding both until the client's
        # transport timeout. Bounded pool + admission semaphore: off the
        # event loop, capped threads, and timed-out waiters never enter
        # the executor queue.
        remaining = remaining_deadline_seconds(run)
        if remaining is not None and remaining <= 0:
            raise RunDeadlineExceeded("run deadline exceeded")

        dataset = run.dataset
        # The dataset read is a synchronous storage call; run it on the
        # bounded pool so a slow backend cannot stall heartbeats (P1) and
        # cannot grow the pool's live threads or queued work (P2).
        if dataset.cases_json:
            cases = json.loads(dataset.cases_json)
        elif remaining is not None:
            try:
                cases = await asyncio.wait_for(
                    _storage_call(state.storage.get_json, dataset.storage_key),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise RunDeadlineExceeded("run deadline exceeded") from None
        else:
            cases = await _storage_call(state.storage.get_json, dataset.storage_key)
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


def _check_deadline(run: Run) -> None:
    """Raise when the run's persisted deadline budget is spent.

    ``None`` (no deadline set, or limit disabled) means unlimited.
    """
    remaining = remaining_deadline_seconds(run)
    if remaining is not None and remaining <= 0:
        raise RunDeadlineExceeded("run deadline exceeded")
