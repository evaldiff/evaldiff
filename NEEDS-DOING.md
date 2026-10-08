# evaldiff — NEEDS-DOING (updated 2026-10-08)

## 0. Current status

The repository is at 0.0.11, with follow-up reliability and hardening fixes
implemented locally but **not committed, published, or deployed**. Local
validation: **50 tests pass**, including 15 new regression tests; lint and
formatting checks pass. PostgreSQL coverage is configured in CI but has not
been run locally. Historical release notes below describe earlier verification,
not the current deployment state.

Next product milestone: **a developer can catch and understand a regression
in five minutes**. Ship the CLI workflow and useful failure reports together,
then add named baselines. Keep detailed security findings in the private tracker.

## 1. Release history

### SHIPPED 0.0.9 (2026-10-02)
- **Quota reservation race fix** (Peter's commit d9c43af, reviewed + verified):
  `create_run` now checks and reserves in ONE conditional `UPDATE` — a stale
  ORM account row can't double-spend quota. `_settle` refunds/charges via a
  relative `UPDATE` too. 2 new tests (4×600 concurrent → only 1 accepted;
  refund must not clobber later reservations). **Live-verified on 0.0.9:**
  600-case run → usage 600/400 immediately while still `queued`; 2nd 500-case
  run → **429**; 2nd signup → **409**.
- CI green on d9c43af: ruff check + format + 22 tests (py3.10/3.11/3.12) + build + npm smoke — 6/6 jobs ✅

### SHIPPED 0.0.8 (security fixes from the P1/P2 audit)
1. **[P1] Signup takeover** — `POST /v1/auth/signup` now returns **409** when
   the email already exists; no key is ever issued for an existing account.
   (Live-verified: 2nd signup with same email → 409, no key.)
2. **[P1] SSRF opt-in moved server-side** — the per-request
   `allow_local_endpoints` field is REMOVED. Policy is now the deployment
   admin's env var only: `EVALDIFF_ALLOW_LOCAL_ENDPOINTS=true`. Callers
   cannot override the server's SSRF restriction. (Live-verified:
   169.254/192.168/127 all 400, public 202, opt-in flag ignored.)
3. **[P1] Diff regression blindness** — `api/diff.py`: a case that passed in
   A but ERRORED (b_passed is None) or is missing in B is now a regression
   ("errored in B" / "missing in B"), not invisible.
4. **[P2] Failed-run stuck in running** — `api/runner.py`: status="failed" +
   error are committed (persisted) inside the except block before the session
   closes; previously the commit inside `_settle()` happened before the status
   assignment, so the terminal state was lost.
5. **[P2] Quota bypass on queued runs** — `api/main.py` create_run: the full
   dataset size is RESERVED at enqueue time (used_cases += case_count, same
   transaction as the Run row), so N queued runs can no longer all pass the
   check. `_settle` swaps the reservation for the actual charge.
6. **[P2] Model failures charged anyway** — `api/runner.py`: only cases that
   executed OK are billed; a run where every call errors is free (0 charged),
   matching the documented refund behavior.

Tests: **18/18 pass, ruff clean** (new: takeover 409, server-side SSRF,
errored-diff regression, failed-run status+refund, quota-reserved-at-enqueue,
erred-cases-not-charged). PyPI 0.0.8 live, Hetzner running,
api.evaldiff.io/health → **version 0.0.8**.

### SHIPPED earlier this cycle (0.0.6 → 0.0.7)
- SSRF endpoint guard (`validate_endpoint` + `_is_blocked_host`, literal-IP
  blocking of private/loopback/link-local/reserved ranges) — 0.0.6.
- /health version from importlib.metadata — 0.0.7.
- GitHub action `allow_local` input — now DEPRECATED (kept for compat;
  server ignores the field since 0.0.8).

## 2. SHIPPED — 0.0.6 → 0.0.7 (all verified ✅)

1. **0.0.6 published to PyPI** (SSRF guard) ✅
2. Hetzner: Dockerfile pin updated, `docker compose build+up api` ✅
   (container runs 0.0.6; /health fix needed one more version — see 0.0.7 below.)
2b. **0.0.7 shipped** (cosmetic: /health read importlib.metadata): PyPI live,
    Hetzner running, **https://api.evaldiff.io/health → version 0.0.7** ✅
3. **Live SSRF verification** (fresh account, 5 cases; 0.0.6 behavior —
   the per-request opt-in was later REMOVED in 0.0.8, see status above):
   - `endpoint → 169.254.169.254` → **400 blocked** ✅
   - `endpoint → 192.168.50.1` → **400 blocked** ✅
   - `endpoint → 127.0.0.1` (no opt-in) → **400 blocked** ✅
   - `endpoint → 127.0.0.1` + `allow_local_endpoints:true` → **202 accepted** (0.0.6; 400 in 0.0.8)
   - `endpoint → api.openai.com` → **202 accepted** ✅
   - **0.0.8 re-verification**: 169.254/192.168/127 all 400 (opt-in flag now ignored), public 202 ✅
4. Pushed both repos ✅ (evaldiff main a04bfb6, action main 7c34df5)
5. Cleanup debug accounts (audit leftovers): delete API keys for
   `action-e2e-*`, `gate-test-*`, `race-*`, `auto-*`, `ssrf-*`, `fix-verify-*`, `dbg-*` @evaldiff.io
   via psql on the db container (or leave; low risk).

## 3. TO DO next (in priority order)

### A. Finish the current reliability release

- [ ] Review and commit the local quota, recovery, HTTPS, and rate-limit fixes.
- [ ] Run CI, including the PostgreSQL quota/recovery tests, before publishing.
- [ ] Publish the release and update deployment pins to the tested version.
- [ ] Follow [the upgrade instructions](deploy/UPGRADING.md): stop old workers
  before starting the updated application and its one-time usage migration.
- [ ] Verify the deployed version and an end-to-end evaluation after rollout.

### B. Next milestone: CLI workflow + actionable failure reports

Ship these two features together:

1. **Finish the CLI workflow.** Implement `evaldiff run` and `evaldiff diff`,
   with authentication, progress, timeouts, and reliable exit codes for CI.
   Align the README quickstart with the implemented commands.
2. **Show why a case failed.** Include input, expected answer, actual output,
   and judge explanation in JSON and Markdown reports. Make the evidence
   available after the run finishes so a failed gate is actionable.

Acceptance: extend the existing forkable demo with one complete CLI example.
A developer should be able to run a passing baseline, deliberately change a
prompt to introduce a regression, get a failing gate, and inspect enough
evidence to diagnose it within five minutes. Put the usage guide in the action
README so the workflow is discoverable.

### C. Following features, in order

1. **Named baselines.** Compare a candidate against the last approved run,
   for example `evaldiff diff --baseline main` (proposed syntax). Record
   dataset, prompt, model, and commit versions so comparisons are reproducible.
2. **Model variability.** Support repeated evaluations and report score spread
   to help users distinguish a regression from a flaky result before blocking
   a release.
3. **Deterministic checks.** Add JSON-schema validation, required fields,
   regex matching, and numeric tolerances for structured-output evaluations.

### D. Later / maintenance

- Validate the core workflow with a few real users before building a large
  dashboard. Learn whether the gate catches problems and whether users trust
  its results enough to block releases.
- `/v1/runs` pagination.
- FastAPI `on_event` → lifespan.
- Continue hardening from the private tracker. Endpoint filtering, model-key
  encryption, and account/signup rate limiting are implemented; release
  verification belongs in section A rather than a new-feature backlog.

### E. Previously completed product work (recorded history)

- **Landing page:** evaldiff.io root live 2026-10-07.
- **Forkable demo:** `evaldiff/example-repo` live with red/green gate verification
  recorded 2026-10-07; extend it for the CLI milestone above.
- **GitHub Action:** `v1` cut; `v2` cut 2026-10-07 with `GITHUB_OUTPUT` handling.
- **npm package:** `@evaldiff/evaldiff` 0.0.9 publication verified 2026-10-08.

## 4. Environment quick-reference
- API: https://api.evaldiff.io (159.69.47.127, Hetzner, deploy@ key-based)
- Auth: header `Authorization: Bearer *** (eval_...)
- Box: ~/evaldiff-deploy/docker-compose.yml, SeaweedFS S3 on :8333 (internal)
- Tests: `.venv/bin/python -m pytest -q` (50 passing locally as of this update)
- Lint / format: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`
- Action: ~/evaldiff-action → repo evaldiff/action (token in .git/config)
- Usage guide (final): /home/peter/evaldiff-guide-auto.md (send as base64!)

## 5. Security hardening (shipped, live-tested)
Hardened and live-verified: signup takeover blocked (409), SSRF literal-IP
guard with server-side policy (callers cannot override), failed-run state
persistence, quota reservation at enqueue (race-safe, 4×600 concurrent
verified), failed-case billing, hashed API keys, SQLi-safe ORM layer,
cross-tenant isolation. Remaining hardening details are tracked privately; the current release
checklist and product priorities are in section 3.
