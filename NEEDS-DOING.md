# evaldiff — NEEDS-DOING (2026-09-30)

Written before sleep; picks up exactly where the security hardening stopped.

## 0. STATUS (as of 2026-10-02 13:00 UTC): 0.0.9 SHIPPED — all 6 audit findings fixed + quota-race fix (d9c43af) ✅

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

### A. Small code nits
1. ~~`/health` version stale~~ FIXED in 0.0.7 (importlib.metadata; live reports 0.0.7) ✅
2. Tag `v1` on evaldiff/action (pushed 7c34df5, tag not yet created).

### B. Known remaining gaps (accepted for v0, fix before scale)
- **Hostname SSRF**: guard only blocks *literal* IPs. `http://metadata.internal`
  or DNS-rebinding hostnames can still resolve to 169.254.169.254. Fix:
  resolve host at validation time, block if ANY A/AAAA record is private
  (watch DNS-rebind: pin resolved IP and connect to it). Medium effort.
- **Model API keys stored plaintext** in `runs.api_key`. Fix: encrypt at rest
  with a box-local key (Fernet + key in env), decrypt only in worker thread.
- **Quota/rate limits**: 1000 runs/account but no per-key RPM cap. A runaway
  script on the free tier can DoS the box (2 vCPU ceiling). Add slowapi or a
  simple token bucket per key.
- **`/v1/runs` list returns all own runs unbounded** — fine now, add pagination later.
- **Deprecation**: FastAPI `on_event` → lifespan (cosmetic).
- **npm package** `@evaldiff/evaldiff` still 0.0.1 (npm 2FA WebAuthn required — Peter must be present for OTP).

### C. Product (when not hardening)
1. **Landing page** on evaldiff.io root — currently nothing served on port 80
   at the apex (Caddy only proxies api.*). One page: what it is, the
   one-line workflow snippet, signup. (claude-design / popular-web-designs skill.)
2. **Forkable demo repo** `evaldiff/example-repo` — tiny JS/Python repo +
   .github/workflows/eval.yml, 3 general-knowledge cases, passes out of the box.
   The "wow, I got a red/green gate in 2 min" moment for the pitch.
3. **Tag `v1` on evaldiff/action** once the allow_local push lands.
4. Usage guide (final, auto-capturing one-liner version) → into the action
   README so it's discoverable on the repo page.

## 4. Environment quick-reference
- API: https://api.evaldiff.io (159.69.47.127, Hetzner, deploy@ key-based)
- Auth: header `Authorization: Bearer *** (eval_...)
- Box: ~/evaldiff-deploy/docker-compose.yml, SeaweedFS S3 on :8333 (internal)
- Tests: `cd ~/evaldiff && .venv/bin/python -m pytest -q` (12 tests)
- Lint: `uvx ruff check api/ tests/`
- Action: ~/evaldiff-action → repo evaldiff/action (token in .git/config)
- Usage guide (final): /home/peter/evaldiff-guide-auto.md (send as base64!)

## 5. Security audit summary (2026-09-30, live-tested)
SAFE: SQLi (ORM only), cross-tenant (404 on foreign ids), SSRF exfil
(responses unparseable → no data leak), prompt injection (data only),
key leakage (not echoed), input validation (422s), hashed API keys,
S3/PG not reachable from public internet.
GAPS: hostname SSRF (B), plaintext model keys (B), no rate limit (B),
SSRF literal-IP guard DONE above.
