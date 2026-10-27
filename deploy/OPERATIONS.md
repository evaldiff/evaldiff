# Production qualification topology

This is the initial topology for Part D qualification, not a measured capacity
claim. Deploy one Compose stack on one host with one replica of each service.
The API port remains internal; Caddy publishes ports 80 and 443. The
`evaldiff-server` entry point explicitly starts one Uvicorn process, containing
one worker scheduler. There is no separate worker container.

| Service | Processes / concurrency | CPU cap | Memory cap |
| --- | --- | --- | --- |
| API + embedded scheduler | 1 API process, 4 concurrent run attempts | 2 CPUs | 2 GiB |
| PostgreSQL | 1 database service | 1 CPU | 1 GiB |
| SeaweedFS | 1 storage service | 1 CPU | 1 GiB |
| Caddy | 1 proxy service | 0.5 CPU | 256 MiB |

Compose enforces these container caps with `cpus`, `mem_limit`, and
`memswap_limit`; memory and swap limits are equal so containers have no
additional swap allowance. CPU caps are ceilings, not reservations. Plan at
least 4 vCPUs and 8 GiB host RAM, leaving headroom for Docker, the OS, builds,
and monitoring. Validate actual usage and host headroom during qualification.
An OOM kill is a qualification failure and requires investigating the workload
or revising and retesting the budget.

The API configuration explicitly allows at most two active and ten queued runs
per account. Four storage threads are allowed per API process. Runs have a
1,800-second overall deadline; the storage caller's wait budget is 15 seconds.
Part C's synchronous-storage cancellation/shutdown limitation remains open;
these settings do not resolve it.

## Rate-limit contract

Authenticated requests share a token bucket per account: 30 requests/minute,
with a burst capacity of 60. Signup uses a bucket per real client IP: 5
requests/minute, with a burst capacity of 5. Both limits are process-local and
reset on process restart. With the single API process they cover traffic to
this stack; they are an anti-abuse backstop, not a durable global quota.
Caddy and Uvicorn's trusted-proxy configuration must preserve real client IPs;
verify this through HTTPS during staging.

Do not scale API replicas or introduce multiple Uvicorn processes under this
contract. Before scaling, either implement shared rate limiting or explicitly
revise the contract to accept independent per-process buckets, then qualify
the new topology. More processes also multiply worker slots and storage pools.
D's two-process startup tests remain required as recovery/concurrency checks;
they do not authorize a multi-process production topology.

## Storage budget

Use a dedicated filesystem of at least 40 GiB for Docker's persistent volumes,
with an initial operational ceiling of 30 GiB used and at least 10 GiB free.
This is a host capacity budget, not an enforced per-volume quota: Compose named
volumes do not impose disk limits. Count PostgreSQL data/WAL, SeaweedFS data,
and Caddy state together. Compose rotates local container logs at 10 MiB with
three files per service (about 120 MiB across four services).
D1 monitoring must alert before the storage ceiling is reached; qualification
must record filesystem capacity, peak usage, and remaining space.

Store backups outside this filesystem and outside the host. Backup cadence,
retention, and capacity planning are defined below.
Record any host or configuration overrides in the qualification evidence;
changes to these budgets require repeating affected load/recovery checks.

## Qualification contract v1

These are initial release gates, chosen before running qualification. They are
not published service guarantees. Freeze this contract, the candidate commit
and image digest, configuration, host specification, and workload seed in the
run manifest before a test begins. A changed target or workload requires a new
run; retain the original result and explain the change. Final qualification
requires Part C's storage cancellation and shutdown fix.

### Workload and measurement

Use the single-stack topology above with production encryption, PostgreSQL,
SeaweedFS, Caddy, and HTTPS model fixtures. Use synthetic data and controlled
endpoints so provider variability does not determine the result. Record fixture
versions and behavior. Do not enable local endpoints in the production candidate;
use publicly routable, controlled HTTPS endpoints for model traffic.

- Provision 10 accounts before timed testing, each with a staging quota of
  100,000 cases. This quota override avoids accidental exhaustion during the
  soak; record it. Retain the production rate limits and scheduling limits.
- Healthy baseline: warm up for 15 minutes, then measure for 60 minutes.
  Submit one 20-case run per minute in total, rotating accounts. Each model
  call returns a fixed 1 KiB response after 200 ms; use deterministic judges
  without external judge calls. Dataset bodies are at most 64 KiB.
- Mixed soak: warm up for 15 minutes, then measure continuously for 24 hours.
  Keep the same arrival rate and 20-case datasets. Of submitted runs, 80% use
  healthy endpoints, 10% use a 2-second response delay, 5% return persistent
  HTTP 503, and 5% stall without returning a body. Use a fixed recorded seed.
  Restart the API container once at hour 6 and once at hour 18 with active runs.
- Large-dataset phase: in addition to the soak, run 10 evaluations sequentially,
  one per account, each with 5,000 cases and a body of at most 8 MiB. Use the
  healthy endpoint. Record completion times and peak resources separately;
  the small-run queue-wait gate does not apply to this phase.
- Fairness/saturation phase: enqueue 10 runs for one account and one run for
  each of the other nine accounts. Use 20 cases and 2-second responses. Verify
  at most four active attempts overall, two per account, and that additional
  submissions beyond an account's queued cap are rejected. Every other
  account's run must begin within 180 seconds; no accepted run may disappear.
- Fault phases: exercise endpoint errors/stalls, database/storage outages,
  worker crash/lease recovery, and slow-drip storage with all storage slots
  occupied. Keep injected faults separate from healthy latency samples.
  After Part C is fixed, stopping the API under occupied storage slots must
  complete within 30 seconds without a forced kill.

During baseline and soak, issue two control-plane requests/second in total,
rotating accounts and respecting each account's rate limit. Use dataset/run
metadata and usage reads plus run submissions. Poll each unfinished run at
most once per 10 seconds and include polling in this traffic budget. If more
polling is needed, replace metadata reads rather than increasing the budget.
Measure upload and report/diff requests separately (at least 100 of each during
baseline, spread across accounts); use reports from completed synthetic runs.

Measure client latency through Caddy, from request start to complete body.
Queue wait is acceptance to first claim, including retries in a separate series;
run execution time is not API latency. Use nearest-rank percentiles, report
sample counts and maximum values, and retain all failures. Sample container and
host memory, disk, queue depth/age, and heartbeat health every five seconds.
Report baseline and soak statistics separately and worst hourly soak values.

### Pass/fail targets

| Measurement | Gate |
| --- | --- |
| Healthy small-run queue wait, baseline and soak outside restart windows | p95 ≤ 15 s; p99 ≤ 60 s |
| Control-plane API latency, baseline and soak outside fault/restart windows | p95 ≤ 500 ms; p99 ≤ 1 s |
| Upload/report/diff latency, ≤ 8 MiB request/response | p95 ≤ 2 s; p99 ≤ 5 s |
| Healthy workload requests | Unexpected 5xx/transport failures ≤ 0.1%; no unexpected 429 |
| Healthy accepted runs | 100% eventually terminal; no unexplained failed runs |
| API memory | Peak container memory ≤ 1.5 GiB |
| PostgreSQL / SeaweedFS / Caddy memory | Peaks ≤ 768 MiB / 768 MiB / 192 MiB |
| Memory stability | Final-hour median ≤ first post-warmup-hour median + 128 MiB per service; no OOM kills |
| Host memory and volume filesystem | Available RAM ≥ 1 GiB; disk used ≤ 30 GiB and free ≥ 10 GiB |
| Restart recovery | Readiness returns within 120 s; abandoned jobs are reclaimed or terminal within 180 s of restart |
| Deadline behavior | Stalled runs become terminal within configured run deadline + 10 s |
| Shutdown with occupied storage slots | Clean exit within 30 s, without forced kill |
| Data/accounting correctness | Zero duplicate committed case results, cross-account access, abandoned reservations, or incorrect settlements |

Injected faults and deliberate rate-limit/cap violations must produce their
specified errors and accounting outcomes; they do not count as unexpected
errors. Report their counts and durations separately. Exclude only the declared
fault intervals and the first 180 seconds after each planned restart from
healthy latency gates; record excluded requests and require recovery gates to
pass. A missing measurement or insufficient samples is an incomplete result,
not a pass. Drain accepted work after load stops; unresolved work after its
persisted deadline plus 180 seconds fails qualification.

### Backup and recovery objectives

- Create a coordinated database/object-storage recovery point twice daily
  (03:00 and 15:00 UTC timers). Maximum interval between successful recovery
  points is 12 hours. To avoid inconsistent dataset references, pause
  submissions and drain/stop writers before capturing both stores; a future
  online scheme must prove equivalent consistency. Record pause duration and
  backup start/completion timestamps.
- Retention matches backup.py exactly: keep all points within seven days,
  one point per day for 30 days, and one point per week for 84 days
  (12 weeks); restic deduplicates overlapping selections. Backups must be
  encrypted, off-host, and checksummed; a point is successful only after both
  stores and its manifest are verified off-host.
- Back up encryption keys separately when provisioned or rotated. Retain every
  required key for as long as a retained recovery point needs it. Manifests
  contain key identifiers, never key values, and include the commit/image digest
  and compatible restore version.
- RPO: at most 24 hours of lost acknowledged writes. Measure incident time
  against the restored point's consistency timestamp. Backup completion delay
  counts against RPO: captures are 12 hours apart, so completing and
  verifying a point keeps at least 12 hours of slack inside the objective
  (a daily cadence could not guarantee 24h end-to-end).
- RTO: at most 60 minutes from declaring recovery to an isolated restored stack
  passing readiness, credential decryption, dataset/report access, and a queued
  evaluation. Include downloading backups, provisioning, and key retrieval.
- Size off-host storage before automation: measure a complete recovery point,
  calculate retained capacity for the policy above, allow at least 25% headroom,
  and record the result. Do not assume compression or deduplication savings.
- Drill against a synthetic incident timestamp after creating known writes.
  Record restored/missing writes, consistency checks, elapsed recovery time,
  and observed RPO. Both stores and matching keys are mandatory for a pass.

Backup automation and aggregate alerts are implemented below. Off-host setup,
notification delivery, load scripts, and measured recovery drills still require
staging evidence; this contract does not claim those gates have passed.

## Readiness and monitoring

`/health` remains a liveness/version endpoint. `/ready` returns 200 only when
fresh dependency samples pass and the enabled embedded scheduler is alive and
has ticked within 30 seconds; otherwise it returns 503. Responses are uncached
and contain only status/check booleans. Compose uses `/ready` for API health
and waits for SeaweedFS health before starting the API.

Every five seconds, the API launches one disposable probe process with a
three-second total I/O budget. It checks database access and gathers aggregate
queue/run/lease counts. With S3 configured it verifies bucket access and a
write/read/delete round trip of a reserved `.evaldiff-readiness/<host>-<pid>`
object (one stable key per API process, avoiding concurrent-probe collisions),
containing only `ok`. Without S3, database health also determines storage
health. S3 credentials need access to that reserved object; a killed probe can
leave it behind, and subsequent probes from that API process overwrite/remove
it. Remove orphaned readiness-prefix objects after retired processes stop. No customer data
is used. Probes are serial, not queued, and timeout/shutdown kills and reaps
the probe process rather than abandoning storage threads. There can be up to
one probe timeout plus interval of detection delay; samples older than interval
plus timeout plus two seconds fail readiness. The request handler uses cached
results and performs no database or network calls.

`/ops` exposes aggregate-only JSON inside the deployment network. Caddy blocks
`/ops` and its subpaths; do not publish the API port or route this endpoint
publicly in an alternative proxy. It reports queue depth/oldest age, active
jobs, expired/missing leases, failed/deadline-expired runs and case errors for
runs finished in the last five minutes, scheduler age, memory, and process-local
error counters. Database/storage runtime failures, dependency probe failures,
claim failures, and heartbeat failures are counted without exception strings,
URLs, keys, account identifiers, or dataset/model contents. Counters reset on
restart; queue/recent-run counts come from the database. These are operational
signals, not a durable audit log or precise billing metrics.

Run the host monitor against the dedicated Docker volume filesystem:

```sh
cd deploy
python3 monitor.py --volume-path /var/lib/docker
python3 monitor.py --loop --volume-path /var/lib/docker
```

A one-shot run returns 0 if there are no alerts and 1 if there are alerts or
collection fails. Loop mode samples about every 15 seconds and emits JSON
records with timestamps, numeric aggregates, resource usage, and alert codes.
Docker commands have eight-second timeouts; a failed collection emits a fixed
critical alert without stderr or exception details. The monitor requires a
Linux host, Python 3.10+, Docker Compose, and permission to access Docker.
It checks all four containers via Docker stats, host available RAM, and the
specified filesystem; it does not require mounting the Docker socket into the
API container.

| Signal | Warning | Critical |
| --- | --- | --- |
| Cached readiness / monitor collection | — | Any failed sample |
| Oldest queued job | ≥ 60 s | ≥ 180 s |
| Queue depth | ≥ 40 | ≥ 80 |
| Failed runs in last five minutes | ≥ 1 | ≥ 5 |
| Timed-out runs in last five minutes | ≥ 1 | ≥ 3 |
| Case errors in runs finished in last five minutes | ≥ 1 | ≥ 10 |
| Expired or missing running-job leases | — | ≥ 1 |
| Runtime/probe database/storage, claim, or heartbeat error counters | Any positive delta | Readiness failure separately |
| API / DB / SeaweedFS / Caddy memory | — | ≥ 1,536 / 768 / 768 / 192 MiB |
| Available host RAM | — | < 1 GiB |
| Volume filesystem free space | ≤ 12 GiB | ≤ 10 GiB or used ≥ 30 GiB |
| Missing container memory measurement | — | Any missing service |

Alerts fire on the first matching sample and repeat while the condition holds;
there is no built-in suppression. Record injected-fault alerts separately in
qualification evidence. A restarted error counter does not produce a negative
delta alert. Resolve a critical dependency/heartbeat alert before accepting new
load; inspect service state using access-controlled logs. Avoid enabling SQL
parameter logging, HTTP body logging, or traceback logging with credentials.

`evaldiff-monitor.service` is a systemd template. Before installing it, adjust
its user, checkout/working-directory and environment-file paths, and volume
filesystem path to the deployment. The user must have Docker access; the `.env`
file must be compatible with systemd EnvironmentFile syntax. Keep it private.
Install/enable the adjusted unit during staging and inspect its output with
`journalctl -u evaldiff-monitor.service`. Configure host journal retention.
Alerts are local structured records in journald; external notification routing
is an operator integration to configure and verify during staging. This code
does not send messages to external services.

Staging must verify the unit stays running, public `/ops` returns 404, dependency
outages produce 503/critical alerts, and the intended alert receiver receives a
test event. Local regression tests cover cached readiness, worker liveness,
probe timeout/cancellation cleanup, aggregate redaction, and alert thresholds;
full-stack routing, container measurements, and notification delivery require
staging evidence before qualification.

## Deployment checks and candidate identity

Run `bash deploy/deploy.sh` on the deployment host after committing the reviewed
candidate and completing the upgrade/backup instructions. The host must already
have Git, Python 3.11+, GNU `timeout`, `tar`, `mktemp`, a working Docker daemon,
and Compose supporting `up --wait --wait-timeout`. The script fails if a
prerequisite is missing; it does not install Docker or edit `/etc/hosts`. Public
DNS and Caddy's HTTPS configuration must already point to this host.

The script derives the full Git revision and version from the clean checkout,
exports build arguments, and builds from a temporary archive of that commit.
Ignored files and concurrent working-tree edits cannot enter the build context.
Temporary build inputs are removed on exit. The Dockerfile bakes the revision
into a Python module before packaging and attaches OCI version/revision labels.
`/health` now includes the baked revision; ordinary source/PyPI installs report
null, and manual builds without candidate arguments report `unknown`. Runtime
environment variables do not override the baked revision. Neither null nor
unknown qualifies for this deployment check.

Compose tags the API image `evaldiff-api:<full-revision>` and does not pull it
from a registry. Before touching services, checks require matching image labels,
a successful encryption-config preflight, and an unchanged clean checkout.
Deployment then stops the old Compose API, waits for healthy PostgreSQL and
SeaweedFS, waits for ready API startup, and starts Caddy after API health passes.
Other API/worker processes must be stopped separately as required by the
upgrade procedure. Credential migration and backups are separate operations.

After startup, every service must have exactly one running container; the
three services with health checks must be healthy. The running API's immutable
local image ID must equal the candidate inspected after build. Container-local
and HTTPS checks both require fresh database/storage/worker readiness and the
expected version/revision. Public `/ops` must return 404. Redirects, invalid
JSON, oversized responses, and certificate failures fail the check. Set
`EVALDIFF_DEPLOY_PUBLIC_URL` to a staging HTTPS origin if needed, and configure
Caddy and DNS for that same origin; paths, URL credentials, and queries are
rejected before service changes.

Commands have bounded waits: build 900 seconds, encryption preflight 30 seconds,
old API stop 45 seconds with a 30-second Docker grace period, and each startup
stage 150 seconds with a 120-second Compose readiness wait. Individual Docker
inspection commands have 10-second budgets. Container smoke has a 10-second
total budget; public smoke tries up to 15 times with 10-second budgets and
2-second pauses, then performs one final diagnostic check. Each HTTP operation
has a 2-second socket timeout and JSON bodies are capped at 64 KiB. The enclosing
process timeout also bounds slow-drip responses. No raw response bodies,
container environments, health-check logs, or exception tracebacks are dumped
by the checker.

A successful run prints expected version/revision and verified local image ID.
Record these in staging evidence. The local image ID is an image configuration
digest, not the registry manifest digest; registry artifact promotion and its
manifest digest remain D3 release work. A failure returns nonzero with the
stage and a fixed diagnostic. Inspect logs privately, resolve the cause, and
rerun qualification. Failures after stopping the old API can leave services
partially updated; there is no automatic rollback or data restoration. Use the
tested rollback procedure. A Docker forced stop is not proof of Part C's clean
shutdown acceptance criterion.

Real-script regression tests use inert Docker/HTTP substitutes to exercise
invalid secrets, old Compose, dirty checkouts, unhealthy dependencies, readiness
failure, wrong labels/running image, and HTTPS revision mismatch without touching
a deployment. A separate wheel check verifies the generated revision module is
packaged and importable. Actual Docker builds, Compose dependency waits, ACME,
and stack-level failure injection still require staging validation.

## Automated coordinated backups

`backup.py` is a host-side Python 3.11+ tool using restic (tested with 0.18.1).
Install restic from its official release and verify its published checksum.
Use `backup.example.json` as the template for `/etc/evaldiff-backup/config.json`.
All configuration, password and optional credential JSON files must be private
(mode 0600), owned by the deployment user. Provision different strong repository
passwords and distinct off-host data/key repositories with separate access
credentials; initialize each repository explicitly using `restic init`.
`off_host_confirmed` is an operator assertion: verify the destinations really
reside on independent hosts; a remote-looking URL alone cannot prove that.
Optional `credentials_file` per repository supplies an environment-variable JSON
mapping for that repository only. The deployment application's environment is
not forwarded to restic. Secure SSH host-key verification and unattended access
before enabling the timer; never disable host-key checking.

Create `/var/lib/evaldiff-backup` and its `staging` child owned by `deploy`, mode
0700, on a filesystem with space for a full capture plus a full downloaded
verification copy. Size retained off-host capacity using the policy above and
25% headroom before activation. Copy the service/timer templates to systemd,
adjust checkout/user paths, run one manual service invocation, inspect its
status/journal, and only then enable the timer. The timer runs twice daily
(03:00 + 15:00 UTC), leaving completion slack for the 24-hour RPO. Runs have
a 50-minute command
budget; systemd supplies a 55-minute outer limit and three minutes for recovery.
Only one backup may run at a time. **Do not deploy, rotate keys, restart services,
or run other database/object writers during capture.** The single-writer
Compose topology and this operational exclusion are requirements of this scheme.

Before pausing writers, the tool exports the running application's primary and
previous Fernet keys into a separate private staging directory, uploads them to
the key repository, restores them, and compares SHA-256 digests. It then stops
the original API and SeaweedFS containers with a graceful 30-second deadline;
a forced kill or OOM aborts capture. PostgreSQL stays running. `pg_dump -Fc`
captures the database, and a network-disabled helper using the exact API image
archives the stopped SeaweedFS named volume through a read-only mount. Both
original containers are restarted even if capture fails; successful capture
also waits for their health checks. The database and volume must have no other
writers. The physical object archive requires the recorded SeaweedFS image on
restore; retain those images in an independently available image registry.

The manifest records consistency time, measured pause duration, database/object
checksums, key fingerprints and the verified key snapshot ID, container image
IDs, API revision and compatible version. It contains no key values. Database,
objects and manifest are uploaded together to the data repository and downloaded
again with `restic restore --verify`; explicit file digests must also match.
Only then is the snapshot tagged `verified`. Retention selects verified data
points by stable deployment host (not temporary capture paths), keeping all
points within seven days, daily points within 30 days and weekly points
within 84 days; restic deduplicates overlapping selections. Pruning follows retention.
Key snapshots are never automatically expired or pruned: retire them manually
only after proving no retained data point needs them. Pending/unverified
snapshots also require operator investigation and controlled cleanup.
See [restic retention](https://restic.readthedocs.io/en/stable/060_forget.html)
and [restore verification](https://restic.readthedocs.io/en/stable/050_restore.html).

The private atomic status file preserves the last successful consistency time
on failure. Journald receives fixed failure-stage codes, never keys, command
stderr, or customer content. The monitor service reports backup failure, missing
status, a run exceeding 55 minutes, and a usable point aged 24 hours. Connect
its critical events to the staging-tested operator notification route. A failed
retention/prune operation fails the job conservatively even when a new verified
snapshot exists. Inspect the repositories and status before recovery.

For an isolated restore, choose a verified data snapshot, restore it and verify
manifest digests, then retrieve the referenced snapshot from the separate key
repository. Supply those exact primary/previous keys, restore the custom dump
with `pg_restore` into a clean PostgreSQL instance, and extract `objects.tar`
into an empty SeaweedFS volume while that service is stopped. Match the recorded
service images and API version/revision. Prove readiness, credential decryption,
dataset/report access and queued execution before declaring recovery complete.
This procedure still needs the timed staging rehearsal required by D2.

Backup pauses count against the existing availability and latency targets;
there is no new exemption. Measure them during qualification. If the graceful
stop/capture scheme cannot meet those targets, revise the capture strategy with
evidence before rollout. A host crash or SIGKILL can prevent automatic restart
and leave private staging files: inspect container state, restart SeaweedFS then
API, remove abandoned helpers/staging captures, and investigate the alert.
Do not treat local script tests as an off-host recovery or RPO/RTO qualification.

## Reproducible qualification tooling

Use [qualification/README.md](qualification/README.md) for the isolated staging
Compose topology, controlled HTTPS fixture, e2e/load/fault scenarios, scoped
backup and restore rehearsal. Copy [qualification/EVIDENCE.md](qualification/EVIDENCE.md)
for each candidate release. Raw latency results are explicitly incomplete until
all contract gates and D2/D3 evidence pass. These tools have local regression
coverage; staging execution and measured recovery/load qualification remain pending.
