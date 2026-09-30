# evaldiff — NEEDS-DOING (2026-09-30)

Written before sleep; picks up exactly where the security hardening stopped.

## 1. DONE this session

- **SSRF endpoint guard** (audit finding #1, the one to fix before launch):
  - `api/main.py`: `validate_endpoint()` + `_is_blocked_host()` block literal-IP
    endpoints in private / loopback / link-local / reserved / multicast ranges
    (covers 169.254.169.254, 10.x, 192.168.x, 127.x, ::1) → HTTP 400.
  - Per-request opt-in: `"allow_local_endpoints": true` in POST /v1/runs
    (for self-hosted model servers), plus global kill-switch setting
    `ALLOW_LOCAL_ENDPOINTS=true` (env) / `Settings.allow_local_endpoints`.
  - No DB migration needed (opt-in is request-scoped, not stored).
  - 2 new tests: blocked-by-default (5 hostile endpoints, public allowed)
    + opt-in works. Full suite: **12/12 pass, ruff clean**.
- **GitHub action** (`~/evaldiff-action`): new `allow_local` input wired
  through action.yml → ED_ALLOW_LOCAL → run body. README updated.

## 2. SHIPPED — 0.0.6 → 0.0.7 (all verified ✅)

1. **0.0.6 published to PyPI** (SSRF guard) ✅
2. Hetzner: Dockerfile pin updated, `docker compose build+up api` ✅
   (container runs 0.0.6; /health fix needed one more version — see 0.0.7 below.)
2b. **0.0.7 shipped** (cosmetic: /health read importlib.metadata): PyPI live,
    Hetzner running, **https://api.evaldiff.io/health → version 0.0.7** ✅
3. **Live SSRF verification** (fresh account, 5 cases):
   - `endpoint → 169.254.169.254` → **400 blocked** ✅
   - `endpoint → 192.168.50.1` → **400 blocked** ✅
   - `endpoint → 127.0.0.1` (no opt-in) → **400 blocked** ✅
   - `endpoint → 127.0.0.1` + `allow_local_endpoints:true` → **202 accepted** ✅
   - `endpoint → api.openai.com` → **202 accepted** ✅
4. Push ~/evaldiff + push ~/evaldiff-action (commit + ls-remote verify)
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
