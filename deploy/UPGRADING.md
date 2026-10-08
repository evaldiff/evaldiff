# Upgrading quota accounting and worker recovery

Stop all API/worker processes from the previous release before starting the
updated application. Take a database backup first. Mixed old/new workers are
not supported during this upgrade: older workers do not honor job leases.

Startup creates the reservation ledger and job-lease tables, then runs an
atomic, one-time usage migration before accepting requests or starting workers.
It reconstructs missing reservations using each run's creation month, preserves
existing ledger entries, and retains legacy usage not attributable to retained
runs as an account balance. An interrupted migration rolls back and is retried
on startup. Historical counters do not contain enough information to reconstruct
exact month-boundary accounting for runs already settled by an older release.

Workers renew leases every 15 seconds. Recovery waits for a lease to expire
(90 seconds without renewal), retains quota for the retry, and fences old
attempts out of result and settlement writes. After two abandoned attempts,
the run fails and its reservation is refunded. A crash can repeat a request to
the external model; local result writes and quota settlement are protected,
but external provider requests cannot be made exactly-once.

Signup limits use the ASGI client's address, which behind a reverse proxy
is the real client only if Uvicorn trusts that proxy. `deploy/docker-compose.yml`
sets `FORWARDED_ALLOW_IPS=172.16.0.0/12` on the `api` service (Caddy is the
only other peer on the default bridge network), so Caddy's
`X-Forwarded-For` is honored and per-IP signup limits key on the real
client. For other topologies set `FORWARDED_ALLOW_IPS` to your proxy's
address range. Keep direct API access private and have the proxy sanitize
forwarded headers — Uvicorn only reads `X-Forwarded-For` from peers in the
trust list, so untrusted callers cannot spoof their IP to evade limits.

## PostgreSQL regression tests

CI runs the quota/recovery tests on both SQLite and PostgreSQL. To run them
locally, install `pytest-asyncio` and `psycopg[binary]`, and set
`EVALDIFF_TEST_POSTGRES_URL` to a disposable PostgreSQL database. Tests create and
drop isolated schemas in that database; the test user needs schema privileges.
