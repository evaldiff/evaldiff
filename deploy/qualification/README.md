# Isolated staging and qualification

These tools prepare D1's reproducible scenarios. They do not establish D2/D3
qualification without measured staging evidence. Final qualification also
requires A–C acceptance. Use a dedicated staging host with public DNS, valid
HTTPS, Docker Compose supporting `--wait`, Python 3.11+, project dependencies
(including httpx), and the exact candidate image already available locally.
No script builds/publishes a candidate or deletes existing data.

## Configure and start

Copy `config.example.json` to a private file (0600). Replace all placeholders;
choose a unique `evaldiff-staging-*` project and a private evidence directory.
Set the reviewed commit, package version, candidate image reference (prefer a
registry digest), and its verified local image ID. The private environment file
must supply `POSTGRES_PASSWORD`, `SEAWEEFS_SECRET`, and a staging Fernet
`EVALDIFF_SECRET_KEY`; use different secrets from production. Keep ten accounts'
quota at 100,000 and retain production limits. The standalone
`deploy/staging-compose.yml` uses project-scoped volumes and routes only the
configured staging hostname. Its resource budgets match production Compose.
Use a dedicated host for ports 80/443 and the dedicated-volume filesystem
contract; do not point tests at production. Requests must receive that project's
Caddy header, and running API image identity must match the configured image ID.

Run the controlled fixture on a separate publicly routable HTTPS host with the
same project dependency environment:

```sh
EVALDIFF_FIXTURE_KEY=... uvicorn deploy.qualification.fixture:app --host 127.0.0.1 --port 9000 --no-access-log
```

Set the key through a private service environment file, not shell history.
Place a TLS proxy in front of it and set `fixture_url` to that HTTPS origin.
Configure the same synthetic key as `fixture_key` in the qualification file.
Do not enable local endpoints on the candidate. The fixture requires this key,
returns 1 KiB after 200ms or 2s, emits controlled 503s, or stalls for an hour.
Wrong credential decryption therefore causes synthetic healthy evaluations to
fail. Verify `/health` and fixture deployment identity separately and attach them
to evidence. No external judge calls are used.

From the checkout root:

```sh
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json up
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json e2e
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json baseline
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json soak
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json large
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json fairness
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json outage --service db
python deploy/qualification/run.py --config /etc/evaldiff-staging/config.json outage --service seaweed
```

`up` rejects a project with existing containers. It starts the exact local image
without building; health, baked version/revision, public `/ops` rejection and
service/package versions are recorded. `e2e` provisions ten accounts (about two
minutes to respect signup limits), persists their keys privately, checks quota,
uploads datasets, executes two evaluations, verifies case sequences, checks
account isolation, and fetches a report and diff. Do not rerun a partial account
provisioning file as a complete setup; inspect it and use a new evidence/project
if necessary. Accounts and datasets are synthetic and deliberately retained.

Baseline and soak each warm up for 15 minutes. Control-plane arrivals total two
requests per second, including one run submission per minute and polling no
more than once per run per ten seconds. Each HTTP request has a ten-second total budget, including slow-drip bodies.
Eight concurrent requests is a host-side safety bound; overload records missed arrivals instead of silently dropping
failures. The baseline uses healthy endpoints. Soak shuffles each seeded block
of twenty runs into 16 healthy, 2 slow, 1 error and 1 stalled run. API restarts
are scheduled at measured hours six and eighteen, waiting for a polled active
run. Stop is graceful, forced kills fail the scenario, and original service
start is attempted even on failure. Requests retain exclusion flags for the
restart interval and first 180s afterward; attach measured readiness and lease
recovery traces separately. The standalone `restart` mode exercises the same
path for a preliminary drill.

Resources and private `/ops` are sampled every five seconds. Failed/late samples
are recorded. Upload/report/diff traffic replaces metadata reads, aiming for at
least 100 of each. Raw events retain status, expected status, latency, workload
and run identifiers, never bearer keys or model/HTTP response content. Account
keys live only in `accounts.json` and `markers.json`, both private. Config and
contract identity are frozen in `manifest.json`; changed identities require a
new evidence directory. Scenarios record actual service and package versions.

Large runs execute sequentially with the same resource sampler; attach measured
completion times and peaks separately from the small-run gates. Fairness checks accepted
runs, cap rejection and other accounts' persisted wait times. Attach sampled or
transactional active-slot bounds and shutdown/fault assertions. An unexpected
cap rejection while enqueueing the prescribed workload fails the scenario;
do not weaken the workload to hide it.

`persisted-runs.json` exports only synthetic-run timing, attempt counts, case
counts and ledger settlement fields using a read-only bounded PostgreSQL query.
**started_at is overwritten on retries**: `latest_claim_wait_seconds` establishes
initial claim wait only for one-attempt runs. Capture first-claim/retry traces
separately for restarted/reclaimed runs; never label latest-claim timings as
initial waits. Latency summaries always say `incomplete`: percentile data alone
cannot qualify accounting, hourly/resource stability, fairness, shutdown or
recovery. Failed phases keep raw records and summaries. Scenarios fail
conservatively on unexpected request failures; retain the measurements for
comparison with the contract's error budget.

Outage scenarios stop only the selected isolated project's dependency, wait
for the cached readiness probe to fail, require HTTPS `/ready` 503, restart the
dependency in a finally block, and require recovery within 120 seconds. Test
notification delivery, client-IP limiting from distinct machines, slow-drip S3,
occupied-storage-slot shutdown and rollback using the cases in EVIDENCE.md.
Those gates remain explicit operator rehearsals, including Part C acceptance;
the harness never claims them from a successful ordinary model run.

## Coordinated restore rehearsal

Run `e2e`, then create a coordinated backup of that staging stack using the
backup tool and its separate off-host repositories. Use the explicit staging
entry point; the backup config's `host` must equal the staging project, and its
repositories/passwords/status path must be staging-specific:

```sh
python deploy/qualification/backup.py \
  --config /etc/evaldiff-staging/config.json \
  --backup-config /etc/evaldiff-backup/staging.json
```

This routes every Compose lookup to the isolated project and preserves the
backup tool's lock, status and verification behavior. Preserve the pre-backup `markers.json` privately,
and record acknowledged synthetic writes after the point to measure loss.

Prepare a **new empty** `evaldiff-staging-restore-*` project/config with the same
candidate identities, its own staging hostname and evidence directory. Supply
the verified full data snapshot ID, backup repository config and an incident
Unix timestamp at or after the point's consistency timestamp:

```sh
python deploy/qualification/restore.py \
  --config /etc/evaldiff-staging/restore.json \
  --backup-config /etc/evaldiff-backup/staging.json \
  --snapshot FULL_64_HEX_SNAPSHOT_ID \
  --markers /var/lib/evaldiff-qualification/source/markers.json \
  --incident-timestamp UNIX_INCIDENT_TIME
```

Restore rejects existing target containers or project-labelled volumes,
unverified snapshots, mismatched identities/digests/keys, and archives with
traversal, links or special files. It downloads both repositories, validates the
manifest, provisions PostgreSQL, restores the custom dump, extracts the physical
SeaweedFS archive into an empty target volume, starts recorded service images,
checks credential decryption and readiness, verifies known dataset/report/run
markers, and executes a new evaluation against restored data. Retain the DB and
SeaweedFS image IDs independently before the drill so Docker can run them.

`restore.env` and `restore-config.json` preserve matching keys privately for the
target. **Do not publish them.** Partial failures deliberately leave the isolated
target for investigation; use a different empty project for a retry. The result
measures total tool elapsed time (including downloads and provisioning) and
incident-to-point age against 60-minute RTO/six-hour RPO. Its pass applies only
to those measured checks: a write-loss inventory and the remaining release gates
are still required. Record any host/key-access preparation that must be added
to real incident RTO. Never treat an already-provisioned drill as evidence that
an unprepared recovery environment can meet the same time.

Copy EVIDENCE.md into the release evidence bundle. Attach redacted measurement
artifacts and results for every gate, keeping secret files in separate custody.
A reviewed, measured final candidate and explicit release decision are required.

## Disposable PostgreSQL qualification

Run the PostgreSQL-enabled suite without Docker using PostgreSQL **16.x** server
binaries already installed in a user-accessible directory:

```sh
python deploy/qualification/postgres.py \
  --bin-directory /path/to/postgresql16/bin \
  --evidence-directory /private/new-postgres-evidence-directory
```

The evidence directory must not already exist. Optional positional pytest node
IDs select a preliminary focused run; omit them for the complete suite. The tool
initializes a private temporary UTF-8 cluster owned by the invoking non-root
user, disables TCP listening, rejects host authentication and allows local trust
only inside its private Unix-socket directory. It creates a disposable database,
sets `EVALDIFF_TEST_POSTGRES_URL` only in the pytest child, and stops the cluster
in a finally block. It never connects to an existing database. Tests have a
15-minute outer timeout; termination kills the pytest process group and attempts
cluster shutdown. Preserve/inspect private diagnostics if cleanup reports failure.

`result.json` records checkout revision/dirty state, hashes of application/test
inputs, Python/server/driver versions, test command, selection, JUnit totals and
explicit PostgreSQL parameterized case count. Private `pytest.log`, `junit.xml`,
and cluster/server logs retain original diagnostics. A dirty checkout result is
preliminary; rerun affected checks for the reviewed final commit/image and
require the existing PostgreSQL CI job before release.

The shared API fixture uses a unique PostgreSQL schema whenever this variable
is set, so ordinary API integration tests now exercise PostgreSQL in that CI
job. Without it they keep SQLite. Recovery and scheduling suites exercise both
backends; explicitly SQLite-specific and custom-app tests retain their intended
backend. The new two-process race covers competing reservations, claims,
expired-lease recovery, reclaims, settlement idempotency and original-month
charging. Existing fresh/legacy simultaneous app startup checks prove the
migration markers and usage totals. These database/process checks do not replace
a real Docker/Caddy/SeaweedFS staging deployment, notification/restore drills,
Part C acceptance or release CI.
