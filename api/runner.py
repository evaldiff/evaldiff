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
_gate: AsyncGate | None = None
_pool_lock = threading.Lock()


class StorageCallTimeout(Exception):
    """The caller's total wait budget for a storage call expired.

    The caller's budget is a total wait bound: botocore's read_timeout
    is a per-socket-read limit (default 60 s), so a peer that keeps
    dribbling bytes can still hold the call — and the pool thread — for
    far longer. The caller can stop waiting, but cannot stop that thread.
    """


class AsyncGate:
    """Process-wide, cross-loop async admission gate (NEEDS-DOING C).

    Bounded by ``capacity``: at most ``capacity`` storage calls are
    admitted into the executor pool at once, across ALL event loops in
    the process — the pool is process-wide, so the admission bound must
    be too (a per-loop semaphore reopens the queue-growth path when a
    new loop is created in the same process).

    Slot accounting (signal, don't assign): ``free`` counts slots not
    yet taken. ``acquire()`` takes a slot atomically under the lock;
    a waiter that cannot take one parks on a future. ``release()``
    (called from the pool's done-callback when work REALLY finishes)
    bumps ``free`` and wakes parked waiters as pure
    signals to take slots — each slot stays in ``free`` until its
    woken waiter actually takes it. A lost or spurious wake therefore
    can neither leak a slot nor double-release one: the waiter simply
    re-checks ``free`` on resume, and a waiter that times out or is
    cancelled has taken nothing, so it has nothing to return.

    Waiters are woken via ``loop.call_soon_threadsafe`` (a worker
    thread releases while the loop thread waits); a closed loop is
    skipped — its slot remains in ``free`` for the next acquirer.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self.free = self.capacity
        self._waiters: list[asyncio.Future] = []
        self._lock = threading.Lock()

    async def acquire(self) -> None:
        while True:
            loop = asyncio.get_running_loop()
            with self._lock:
                if self.free > 0:
                    self.free -= 1
                    return
                # Park: append under the lock (inline — no nested lock
                # acquisition, which would self-deadlock on a non-
                # reentrant threading.Lock).
                fut: asyncio.Future = loop.create_future()
                self._waiters.append(fut)
            # Wake signal, or timeout / cancellation. The finally below
            # removes the parked future whatever the outcome; we never
            # take a slot here, so there is nothing to release on exit.
            try:
                await fut
            finally:
                with self._lock:
                    if fut in self._waiters:
                        self._waiters.remove(fut)
            # Woken — loop and re-check free. Spurious wakes are
            # harmless: the slot stays in ``free`` until taken.
            continue

    def _wake_one(self, fut: asyncio.Future) -> None:
        # Runs on the loop thread. The waiter may have been cancelled
        # between the done() check in release() and this hop — a set on
        # a cancelled/done future would raise and crash the loop, so
        # suppress it. Other waiters are also signalled, so cancellation
        # cannot strand an available slot.
        with contextlib.suppress(asyncio.InvalidStateError):
            if not fut.done():
                fut.set_result(True)

    def release(self) -> None:
        """One admitted call finished: free its slot and wake parked
        waiters (called from the pool's done-callback,
        exactly once per admitted submission).

        The freed slot goes back to ``free`` (NOT to any waiter
        directly): woken waiters are re-checking signals, and a slot
        stays available until one of them actually takes it. A lost or
        spurious wake therefore can neither leak a slot nor
        double-release one, and a waiter that timed out or was
        cancelled has taken nothing — so it has nothing to return. Wake
        every parked waiter: if one is cancelled before its callback
        runs, another can still claim the free slot.
        """
        with self._lock:
            if self.free < self.capacity:
                self.free += 1
            alive: list[asyncio.Future] = []
            for fut in self._waiters:
                if fut.done():
                    continue  # timed out / cancelled: drop it
                loop = fut.get_loop()
                if loop.is_closed():
                    continue  # closed loop: drop it, slot stays in free
                try:
                    loop.call_soon_threadsafe(self._wake_one, fut)
                except RuntimeError:
                    # Loop closed between the check and the hop: drop
                    # the waiter; the slot stays in free.
                    continue
                alive.append(fut)
            self._waiters = alive


def _storage_pool() -> tuple[ThreadPoolExecutor, AsyncGate]:
    """Bounded pool + process-wide admission gate for synchronous storage
    calls (NEEDS-DOING C).

    A stalled S3 call occupies a thread until it actually returns and
    cannot be cancelled. Two bounds limit its impact:

    - the pool caps LIVE threads (``max_storage_threads``);
    - the admission gate (PROCESS-WIDE, shared by every event loop) is
      acquired BEFORE ``pool.submit``, so a call whose waiter times out
      while waiting for a slot never enters the executor's UNBOUNDED
      internal queue — the old submit-then-wait pattern let 100
      timed-out submissions queue 100 work items behind one blocked
      thread, growing memory without limit;
    - each caller has a total wait budget (``_storage_call``), so it can
      settle its run while the synchronous call remains in a pool thread.
      Socket timeouts do not impose a total lifetime on that thread.

    The slot is released exactly once, on the WORKER thread (a
    done-callback on the concurrent.futures.Future, no loop hop) — NOT
    when the budget lapses and NOT when a waiter times out. Releasing
    early would admit a replacement call right back into the stalled
    pool's queue, which is exactly the leak being closed; and a timed-out
    waiter never took a slot, so it has nothing to return.
    """
    global _pool, _pool_size, _gate
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
            _gate = AsyncGate(_pool_size)
        return _pool, _gate


async def _storage_call(fn, *args, timeout: float | None = None) -> object:
    """One storage call: admitted off-loop, with a bounded caller wait.

    Admission is process-wide and happens before the work item is
    enqueued; the slot is released when the worker thread finishes (see
    ``_storage_pool``). ``timeout`` bounds the caller's wait, not the
    synchronous operation's lifetime.
    """
    _pool, gate = _storage_pool()
    await gate.acquire()

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    # If the caller gave up first (deadline / cancellation), the error
    # we resolve later would otherwise sit un-retrieved and log a
    # warning — mark it retrieved eagerly.

    def _reap(_f: asyncio.Future) -> None:
        if _f.cancelled():
            return
        with contextlib.suppress(BaseException):
            _f.exception()

    fut.add_done_callback(_reap)

    def _resolve(result: object | None = None, error: BaseException | None = None) -> None:
        # Runs on the loop thread; whichever of the worker or timer fires
        # first wins, and the other is a no-op.
        if fut.done():
            return
        if timer is not None:
            timer.cancel()
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    def signal(*, result: object | None = None, error: BaseException | None = None) -> None:
        # Thread-safe completion from the pool thread. A closed loop
        # means its caller is gone; the pool callback still frees the slot.
        if fut.done():
            return
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(_resolve, result, error)
        except RuntimeError:
            # Loop closed between the check and the hop.
            pass

    timer = (
        loop.call_later(
            timeout,
            _resolve,
            None,
            StorageCallTimeout(f"storage call exceeded its total wait budget ({timeout}s)"),
        )
        if timeout is not None and timeout > 0
        else None
    )
    try:
        cfut = _pool.submit(fn, *args)
    except BaseException:
        if timer is not None:
            timer.cancel()
        # submit failed: the work never started, so no one will release
        # the slot — hand it back now (the worker-finished release path
        # is the only other one that ever fires for this call).
        gate.release()
        raise

    def _release(_done: object) -> None:
        # Fires in the worker thread when the call REALLY finishes (or
        # errors). gate.release() is thread-safe (its own lock) and must
        # run here — NOT hopped onto a possibly-closed loop — so the
        # slot is never lost to a loop that is shutting down. Waking
        # parked waiters on other loops is safe against a closed loop
        # (the hop is unwound and that waiter's own timeout path owns
        # its exit).
        try:
            try:
                result = _done.result()
            except Exception as exc:  # noqa: BLE001 — storage clients raise many error types
                signal(error=exc)
            else:
                signal(result=result)
        finally:
            gate.release()

    cfut.add_done_callback(_release)
    try:
        # The event-loop timer can end the caller's wait while the pool
        # thread is still blocked; the pool future is the release latch.
        return await fut
    except StorageCallTimeout:
        # Surface the caller's elapsed budget as a timeout so deadline
        # guards discard the result. The pool thread may still be running.
        raise asyncio.TimeoutError(
            f"storage call exceeded its total wait budget ({timeout}s)"
        ) from None
    finally:
        if timer is not None:
            timer.cancel()


def _storage_call_budget(remaining: float | None) -> float | None:
    """Maximum caller wait for one storage call (seconds).

    The transport's connect/read timeouts only bound the time between
    socket reads, so the caller's budget is the per-call cap; a run
    deadline narrows it further. The synchronous operation may continue
    after its caller stops waiting (NEEDS-DOING C).
    """
    settings = state.settings
    budget = float(getattr(settings, "storage_timeout", 15.0)) if settings else 15.0
    if remaining is not None:
        budget = min(budget, float(remaining))
    return budget if budget > 0 else None


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
    off the event loop (bounded pool + process-wide admission gate) so a
    slow backend cannot stall lease heartbeats; the deadline bounds the
    wait, and a write that lands after the deadline does not charge the
    case — it raises RunDeadlineExceeded instead."""
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
                    _storage_call(
                        state.storage.put_json,
                        key,
                        payload,
                        timeout=_storage_call_budget(remaining),
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                # Deadline lapsed (or the caller's storage wait budget
                # expired) while the write was in flight (or
                # waiting for a slot): the in-flight write may still
                # complete in its thread, but it is DISCARDED — the case
                # is not charged.
                raise RunDeadlineExceeded("run deadline exceeded") from None
        else:
            # Unlimited budget (or none left, caught by the caller): still
            # off the event loop, with a bounded caller wait even though
            # a slow backend may continue occupying the thread.
            output_key = await _storage_call(
                state.storage.put_json,
                key,
                payload,
                timeout=_storage_call_budget(None),
            )
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
                    _storage_call(
                        state.storage.get_json,
                        dataset.storage_key,
                        timeout=_storage_call_budget(remaining),
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                # Deadline lapsed (or the caller's storage wait budget
                # expired) while the read was in flight: the
                # run fails and is refunded — the worker slot is freed.
                raise RunDeadlineExceeded("run deadline exceeded") from None
        else:
            # No deadline on this run: still bound the caller's wait.
            cases = await _storage_call(
                state.storage.get_json,
                dataset.storage_key,
                timeout=_storage_call_budget(None),
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


def _check_deadline(run: Run) -> None:
    """Raise when the run's persisted deadline budget is spent.

    ``None`` (no deadline set, or limit disabled) means unlimited.
    """
    remaining = remaining_deadline_seconds(run)
    if remaining is not None and remaining <= 0:
        raise RunDeadlineExceeded("run deadline exceeded")
