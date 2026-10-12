# Upgrading quota accounting and worker recovery

Stop all API/worker processes from the previous release before starting the
updated application. Take a database backup first. Mixed old/new workers are
not supported during this upgrade: older workers do not honor job leases.

Startup serializes schema creation and one-time migrations on one database
connection and transaction before accepting requests or starting workers.
PostgreSQL uses a database-scoped transaction advisory lock with a 30-second
lock timeout; SQLite uses `BEGIN IMMEDIATE` with its configured 30-second
write-lock wait. A failed startup rolls back schema/migration changes and
releases the lock. Migration helpers cannot commit the outer transaction.
The deadline-column migration precedes the legacy usage backfill so older
databases can be read using the current Run model.

The usage migration reconstructs missing reservations using each run's creation month, preserves
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

## Deployment topology and budgets

See [OPERATIONS.md](OPERATIONS.md) for the single-process deployment contract,
explicit concurrency and rate limits, and initial CPU/memory/storage budgets.
These defaults require staging and load qualification before release.

## PostgreSQL regression tests

CI runs the quota/recovery tests on both SQLite and PostgreSQL. To run them
locally, install `pytest-asyncio` and `psycopg[binary]`, and set
`EVALDIFF_TEST_POSTGRES_URL` to a disposable PostgreSQL database. Tests create and
drop isolated schemas in that database; the test user needs schema privileges.

## Production credential encryption (Part B)

`EVALDIFF_PRODUCTION=true` requires a valid, stable `EVALDIFF_SECRET_KEY`.
Startup validates configuration before opening the database, and checks all
stored model credentials before serving traffic or starting workers. Plaintext
credentials, unreadable ciphertext, and a legacy bounded PostgreSQL credential
column prevent production startup. An empty provider credential is allowed for
endpoints that need no authentication. Development defaults to
`EVALDIFF_PRODUCTION=false`: without a key, new credentials remain plaintext and
startup logs a warning. Even development refuses an explicitly invalid key or
unreadable existing ciphertext. Settings are captured at startup; restart all
processes after a configuration change.

The production Compose service always enables production mode and requires the
key. The image now builds from the repository checkout (build context is the
repository root), **not the old PyPI pin**. Deploy the complete checkout, not
only a copied `deploy/` directory. `.dockerignore` allows only package inputs;
`.env`, databases, and backups do not enter the build context. Do not deploy a
mixed fleet of old and new API/workers.

### Provisioning and key custody

Generate a key once using a Python environment with this package installed.
Run this from `deploy/`; it creates a private file and refuses to overwrite an
existing key. The key is not printed or placed in shell history:

```sh
umask 077
python - <<'PY'
import os
from api.secrets import generate_secret_key
fd = os.open('encryption.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('EVALDIFF_SECRET_KEY=' + generate_secret_key() + '\n')
PY
```

Securely merge that assignment into the deployment's existing `.env` (mode
0600), alongside `POSTGRES_PASSWORD` and `SEAWEEFS_SECRET`. Do not regenerate
it on upgrades. Never commit either file, run `set -x`, print rendered Compose
configuration, or paste the key in logs/tickets. `docker compose config --quiet`
validates without displaying secrets. Store a separate access-controlled backup
of the key in a password manager or equivalent secure store. Record which key
set accompanies each database backup without recording keys in the manifest.
Losing all matching keys makes encrypted credentials unrecoverable.

### Existing deployment: backup, migrate, verify

Commands below run from the checked-out `deploy/` directory. Use a maintenance
window; stop **every** API/worker replica, including processes outside Compose.
Provision the primary key (and any required old keys) before running Compose.

```sh
docker compose config --quiet
docker compose build api
docker compose run --rm --no-deps api python -m api.migrate_secrets --validate-config
docker compose stop api
# Keep PostgreSQL available; do not start workers yet.
docker compose up -d db
umask 077
docker compose exec -T db pg_dump -U evaldiff -d evaldiff -Fc > pre-encryption.dump
docker compose run --rm --no-deps api python -m api.migrate_secrets
docker compose run --rm --no-deps api python -m api.migrate_secrets --check
docker compose up -d api
```

Retain the database dump and matching encryption keys securely and separately.
Back up object storage as well when preparing a complete-stack restore. Do not
remove old backups just because migration succeeds: rehearse restoration first.
A fresh empty database needs no migration; startup creates the new schema.
`bash deploy.sh` validates the candidate's encryption configuration before
starting services, but deliberately does **not** silently migrate existing data.
The script checks API health inside the container and over HTTPS through Caddy.

The migration locks `runs`, widens PostgreSQL `api_key_ref` from `varchar(512)`
to `text`, encrypts every nonempty plaintext credential, and validates existing
ciphertext. Schema and data updates commit atomically on PostgreSQL; SQLite data
updates use `BEGIN IMMEDIATE`. SQLite's existing `varchar(512)` does not enforce
that length, so no table rebuild is needed; new databases use `TEXT`. Larger
provider keys and their Fernet expansion fit without truncation. Migration is
idempotent: existing ciphertext is not double-encrypted. A failure rolls back
all changes; fix the configuration and rerun. Output contains only counts or
sanitized errors. The reserved `enc:v1:` prefix identifies stored ciphertext;
a legacy plaintext provider key starting with that prefix requires manual,
controlled remediation rather than guessing whether an unreadable token is plaintext.

For SQLite, stop all writers and use its backup API (not a raw file copy of a
live WAL database), then run the same migration module with the database URL
and key in the environment:

```sh
python - <<'PY'
import sqlite3
with sqlite3.connect('/absolute/path/evaldiff.db') as source:
    with sqlite3.connect('/secure/backup/pre-encryption.db') as target:
        source.backup(target)
PY
python -m api.migrate_secrets
python -m api.migrate_secrets --check
```


### 0.0.18+: run-wide deadlines (Part C)

`runs` gains a `deadline_at` column. `create_all` cannot alter an existing
table, so startup runs a one-off, idempotent migration
(`migrate_run_deadline_column`, marker row `run_deadline_v1` in
`quota_migrations`). No manual step is required: back up the database as
usual, stop old replicas, start the new image — the column is added before
the worker starts. After upgrade, verify with:

```sh
docker compose exec -T db psql -U evaldiff -d evaldiff \
  -c "select column_name from information_schema.columns
      where table_schema='public' and table_name='runs' and column_name='deadline_at'"
```

### Restore and rollback

Restore into an isolated stack first; never test recovery against the live DB.
Use the matching key set, keep workers stopped, and run `--check` before enabling
them. For a PostgreSQL dump, on that isolated stack:

```sh
docker compose stop api
docker compose exec -T db pg_restore --clean --if-exists --no-owner \
  -U evaldiff -d evaldiff < pre-encryption.dump
# Plaintext/pre-upgrade backups need migration before the production check.
docker compose run --rm --no-deps api python -m api.migrate_secrets
docker compose run --rm --no-deps api python -m api.migrate_secrets --check
```

Confirm a queued run completes using the expected provider credential and that
new runs store ciphertext. A structurally valid but wrong key must fail `--check`
and startup without printing credentials. For SQLite restore a backup via the
backup API into a new database path and point the isolated app at that path.

If rollout must be rolled back, stop all new API/workers, restore the
pre-upgrade database plus its matching key set, then deploy the recorded previous
application version and configuration. Restoring an older backup loses writes
made since that backup, so keep the maintenance window until validation passes.
Do not attempt to shrink `TEXT` to 512 characters or decrypt rows in place as a
rollback shortcut. After rotation, an older application that supports only one
key cannot read a mixed-key database; restore its matching backup instead.

### Rotation without losing access

1. Back up the database and the existing key set; stop all API/workers.
2. Provision a newly generated `EVALDIFF_SECRET_KEY`. Put the old primary and
   any still-needed older keys in `EVALDIFF_PREVIOUS_SECRET_KEYS`, comma-separated,
   using the same protected provisioning mechanism. All new writes use the new
   primary; reads accept every configured key.
3. Rebuild/preflight the candidate if necessary, then run:

   ```sh
   docker compose run --rm --no-deps api python -m api.migrate_secrets --rotate
   docker compose run --rm --no-deps api python -m api.migrate_secrets --check
   # Prove that no stored row still needs an old key, without changing .env yet.
   docker compose run --rm --no-deps -e EVALDIFF_PREVIOUS_SECRET_KEYS= api \
     python -m api.migrate_secrets --check
   ```

4. Only after the primary-only check succeeds, remove the previous-key setting
   from the runtime environment and recreate all API/workers. Retain old keys
   in secure backup storage for as long as any retained backup requires them.
   A failed migration leaves the database unchanged; keep the fallback keys,
   resolve the error, and rerun. Repeating `--rotate` does not rewrite tokens
   already encrypted with the primary.

The test suite covers missing/invalid configuration, long values, atomic rollback,
idempotency, rotation, and SQLite backup restoration with queued-run execution.
Migration/rotation tests also run against PostgreSQL when
`EVALDIFF_TEST_POSTGRES_URL` is set, including the legacy `varchar(512)` upgrade.

