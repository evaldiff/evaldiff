# evaldiff — NEEDS-DOING (updated 2026-10-10)

## 0. Current status

Package metadata is at **0.0.13**. The current code includes run pagination,
FastAPI lifespan startup, and the review fixes for plain-HTTP response limits,
invalid run comparisons, and duplicate rubric verdicts. Latest local validation:
**68 tests passed**, including 16 review regression tests; lint and formatting
checks passed. PostgreSQL-specific validation and the deployed state were not
verified in this review. Historical release notes below are historical evidence,
not confirmation of the current deployment.

Readiness assessment: suitable for a controlled internal pilot with trusted
endpoints after deployment checks; **public production readiness is not yet
established**. Complete sections 3A–3D before public rollout. Section 3E is also
required before advertising the complete CLI/CI workflow.

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

### A. Bound model and judge responses over HTTP and HTTPS

Files: `api/runner.py`, `api/judges.py`, `api/settings.py`, `api/ssrf_guard.py`.

- [ ] Add one shared streamed-response reader for model and judge calls, with a
  configurable byte limit. Enforce it before JSON parsing for both HTTP and
  HTTPS; retain TLS certificate verification and the dial-time SSRF guard.
- [ ] Bound decoded response bytes as well as wire bytes so compression cannot
  bypass the memory limit. Reject oversized declared lengths early, but never
  rely solely on `Content-Length` (it can be missing or inaccurate).
- [ ] Close responses promptly on limit errors and cancellation. Record a clear
  case error and avoid retrying deterministic size violations or charging the
  failed case. Keep the proxy's existing plain-HTTP cap as an additional bound.

Acceptance: integration tests cover HTTP and HTTPS, model and judge responses,
chunked and EOF-delimited bodies, compressed payloads, and oversized headers.
Valid responses at the limit succeed; excessive responses fail without an
unbounded allocation. A subsequent queued run can still complete.

### B. Require encryption in production deployments

Files: `api/secrets.py`, `api/settings.py`, `api/main.py`,
`deploy/docker-compose.yml`, `deploy/deploy.sh`, `deploy/UPGRADING.md`.

- [ ] Add an explicit production configuration that refuses startup when
  `EVALDIFF_SECRET_KEY` is missing or invalid. Preserve documented local
  development behavior without silently allowing plaintext in production.
- [ ] Pass the key into the API container and validate required configuration
  before deployment. Document generating and securely provisioning a stable
  key; never commit it or include it in logs.
- [ ] Provide an idempotent migration for existing plaintext model keys, with
  backup and rollback instructions. Check encrypted values fit the database
  column, including longer provider keys.
- [ ] Document key backup, restoration, and rotation. Rotation must retain
  access to previously encrypted values until migration is complete.

Acceptance: production startup fails for missing/invalid keys; new and migrated
rows contain ciphertext; queued runs still execute after restart. Restore a
backup with its matching key and verify decryption. Test wrong-key handling
without exposing secrets.

### C. Bound run duration and prevent queue starvation

Files: `api/worker.py`, `api/runner.py`, `api/leases.py`, `api/quota.py`,
`api/settings.py`.

- [ ] Add configurable overall run deadlines in addition to per-request
  timeouts. Define the budget across retries and reclaimed attempts so a crash
  cannot reset it indefinitely.
- [ ] On deadline expiry, cancel outstanding calls, release resources, persist
  a terminal failure, and settle quota once according to the documented policy.
  Preserve lease fencing when timeout, heartbeat loss, and recovery race.
- [ ] Add bounded worker concurrency and per-account active/queued limits.
  Schedule fairly across accounts so one large backlog cannot monopolize all
  slots. Keep blocking database/storage work from starving lease heartbeats.
- [ ] Preserve atomic claiming, independent database sessions per task, and
  graceful shutdown. Start with conservative defaults and tune from load tests.

Acceptance: a stalled endpoint reaches its deadline, resources close, and quota
settles exactly once. Another account's fast run completes while the slow run
is active. Concurrent workers never duplicate committed case results; shutdown
and lease expiry recover work without stale writes or abandoned reservations.

### D. Verify the production stack and prepare operations

Files: `.github/workflows/ci.yml`, `tests/test_recovery_quota.py`, `deploy/`.
Depends on A–C for final release qualification.

- [ ] Run the complete suite against PostgreSQL using
  `EVALDIFF_TEST_POSTGRES_URL` and a disposable database. Record the tested
  commit, database/driver versions, commands, and results. Require the existing
  PostgreSQL CI job to pass before release.
- [ ] Exercise concurrent quota reservations, settlement, month boundaries,
  lease recovery, and legacy migration on PostgreSQL. Test the intended number
  of app/worker processes and simultaneous startup; serialize migrations if
  concurrent startup exposes a race.
- [ ] Deploy the exact candidate artifact to staging with PostgreSQL,
  SeaweedFS, Caddy, and production settings. Verify signup, encrypted keys,
  dataset upload, HTTPS model calls, diff/report output, and real client-IP
  rate limiting end to end.
- [ ] Correct deployment smoke checks: the API port is not published to the
  host, so check it inside the container and check HTTPS through Caddy.
  Fail deployment clearly when dependencies or readiness checks fail.
- [ ] Add readiness checks and monitor queue age/depth, run failures/timeouts,
  worker heartbeat health, memory, and database/storage errors. Redact keys and
  avoid logging sensitive dataset/model payloads.
- [ ] Automate database and object-storage backups; preserve the encryption key
  separately. Restore into an isolated stack and verify data and key access.
  Record recovery-time and data-loss targets and the measured restore result.
- [ ] Run a load/soak test with mixed accounts, large datasets, slow/failing
  endpoints, and worker restarts. Agree on queue-wait, API-latency, and memory
  limits before the test; record whether the candidate meets them.
- [ ] Publish the tested version and update the Dockerfile pin (it installs a
  PyPI release, not the local checkout). Follow the upgrade instructions,
  verify the deployed version, and document a tested rollback procedure.

Acceptance: all required CI checks and staging scenarios pass, resource usage
stays within the agreed budget, and restore/recovery drills succeed. Attach
results to the release; do not infer production validation from SQLite tests.

### E. Finish the CLI workflow and actionable reports

Files: `evaldiff_cli/main.py`, `tests/test_cli.py`, `api/main.py`, `api/diff.py`,
`api/storage.py`, `README.md`. Can proceed alongside A–D.

- [ ] Implement `evaldiff run` with authentication, dataset upload, submission,
  bounded polling, rate-limit handling, and useful progress/error messages.
- [ ] Implement `evaldiff diff` with the API's comparison requirements. Define
  and document stable exit codes: 0 for a passing evaluation/gate, 1 for a
  failed evaluation/regression, and 2 for operational or usage errors.
  Incomplete/failed runs and polling timeouts must never produce a green gate.
- [ ] Include input, expected answer, actual output, and judge explanation in
  authorized JSON/Markdown reports. Persist evidence across restarts, including
  deployments without S3; bound report size and preserve account isolation.
- [ ] Align README commands, authentication setup, threshold semantics, and
  version output with the implementation. Clearly label npm's stub status.
- [ ] Extend the forkable demo and action usage guide with a complete CLI
  example: passing baseline, deliberate regression, failing gate, and report.

Acceptance: execute the documented quickstart from a clean install. CLI tests
cover passing and failing cases, authentication errors, rate limits, network
failures, invalid comparisons, and polling deadlines. A developer can diagnose
an intentional regression from the resulting report in five minutes.

### F. Following features and maintenance

- Named baselines with dataset, prompt, model, and commit provenance.
- Repeated evaluations and score spread to distinguish regressions from noise.
- JSON-schema, regex, required-field, and numeric-tolerance checks.
- Validate the workflow with real users before building a larger dashboard.
- Continue hardening from the private tracker; the plan above is a response to
  this review, not an exhaustive security certification.

### G. Previously completed product work (recorded history)

- **Landing page:** evaldiff.io root live 2026-10-07.
- **Forkable demo:** `evaldiff/example-repo` live with red/green gate verification
  recorded 2026-10-07; extend it for the CLI milestone above.
- **GitHub Action:** `v1` cut; `v2` cut 2026-10-07 with `GITHUB_OUTPUT` handling.
- **npm package:** `@evaldiff/evaldiff` 0.0.9 publication verified 2026-10-08.

## 4. Environment quick-reference
- API: https://api.evaldiff.io (159.69.47.127, Hetzner, deploy@ key-based)
- Auth: header `Authorization: Bearer *** (eval_...)
- Box: ~/evaldiff-deploy/docker-compose.yml, SeaweedFS S3 on :8333 (internal)
- Tests: `.venv/bin/python -m pytest -q` (68 passed in the latest local run; PostgreSQL not verified)
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
