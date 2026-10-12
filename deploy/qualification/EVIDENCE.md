# Release qualification evidence

Candidate version: **pending**  
Candidate commit: **pending**  
API local image ID / registry manifest digest: **pending / pending**  
Contract SHA-256 / workload seed: **pending / pending**  
Harness/fixture commit and hashes: **pending**  
Qualification status: **INCOMPLETE**

Fill every row with an artifact link, measured result, timestamp and reviewer.
A script exiting zero is a completed scenario, not a complete release verdict.
Keep original failed runs; fixes or changed contracts require new evidence.
Never attach account keys, restore.env, configuration credentials or key bundles.

| Gate | Evidence / result | Status |
| --- | --- | --- |
| A–C acceptance, including storage cancellation and occupied-slot shutdown | | Pending |
| Required CI, complete PostgreSQL suite, concurrent startup/migrations | | Pending |
| Exact candidate API/service identities and dependency versions | manifest.json, stack.json | Pending |
| Host CPU/RAM/dedicated volume capacity and configuration overrides | | Pending |
| HTTPS signup, encryption, datasets, run, cases, report, diff, account isolation | events.jsonl | Pending |
| Client-IP limiting from two independent clients, forwarded-header spoofing | | Pending |
| Baseline: 15-minute warmup, 60-minute measurement, sample counts and percentiles | baseline-summary.json, events.jsonl | Pending |
| Soak: 15-minute warmup, 24 hours, seed/mix and worst hourly gates | soak-summary.json, events.jsonl | Pending |
| Initial claim queue waits, retry waits separately, all accepted runs terminal | persisted-runs.json and claim/retry traces | Pending |
| At least 100 upload, report and diff samples; latency gates | | Pending |
| Memory peaks/stability, host RAM/disk, OOM, queue/heartbeat and errors | resource events, first/final hour medians | Pending |
| Restarts at hours 6/18 with active runs; readiness ≤120s and lease recovery ≤180s | restart and recovery traces | Pending |
| Large datasets: ten sequential 5,000-case runs, timings and peak resources | | Pending |
| Fairness: caps, rejection, nine other accounts start ≤180s; no lost runs | fairness events, persisted-runs.json, active-slot traces | Pending |
| Database/storage outages and alert/notification delivery | outage events and notification receipts | Pending |
| Slow-drip storage and occupied-slot clean shutdown ≤30s; deadline outcomes | | Pending |
| Zero duplicate results, invalid settlements or abandoned reservations | persisted-runs.json and PostgreSQL assertions | Pending |
| Off-host retention capacity +25%, verified data/keys, key custody | | Pending |
| Restore: data, reports, decryption and execution; RPO ≤6h, RTO ≤60min | restore-result.json, lost-write inventory | Pending |
| Rollback: compatible versions/keys/schema, matching stores and lost writes | | Pending |
| Independent patch review and final artifact evidence rechecked after changes | | Pending |
| Published and deployed exact qualified image; readiness/HTTPS/identity | | Pending |

Incident timestamp: **pending**  
Recovery consistency/completion timestamps: **pending**  
Acknowledged writes restored / lost since point: **pending / pending**  
Observed RPO / full RTO (including provisioning/download/key retrieval): **pending / pending**

Record declared fault intervals and restart exclusions, including request counts
and durations. Backup maintenance has no automatic exclusion. Include all
failures, late/missing samples and transport errors. Apply the gates in
OPERATIONS.md; missing measurements and insufficient samples remain incomplete.

Release decision: **INCOMPLETE**  
Operator / reviewer / UTC decision timestamp: **pending**
