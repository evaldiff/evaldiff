# Evaldiff

[![evaldiff CI](https://github.com/evaldiff/evaldiff/actions/workflows/ci.yml/badge.svg)](https://github.com/evaldiff/evaldiff/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/evaldiff)](https://pypi.org/project/evaldiff/)
[![npm version](https://img.shields.io/npm/v/@evaldiff/evaldiff)](https://www.npmjs.com/package/@evaldiff/evaldiff)

> **CI for LLM prompts.** Datasets, eval runs, and regression diffs as a
> pass/fail gate for your CI — the `npm test` of prompts.

Evaldiff scores your LLM output against a dataset of expected answers and
rubric criteria, then gives you a green/red gate you can wire straight into
GitHub Actions. When a model change or prompt edit regresses a case, the diff
tells you exactly which one.

## Why

You already know how to test normal software. LLM output is non-deterministic,
so "it worked yesterday, why not today?" is a daily problem. Evaldiff sells the
**green checkmark** — not "AI testing" as an abstract service.

- **Dataset** — a JSON file of `{input, expected, rubric?}` cases.
- **Run** — score a (dataset, model, prompt) combination; per-case score,
  pass/fail vs. a threshold, latency, tokens, cost.
- **Diff** — `run A vs B`: regressions highlighted, side by side.
- **Gate** — `evaldiff run` / `evaldiff diff` exit `0` = pass, `1` =
  regression/failure, `2` = operational error (auth, deadline, incomplete run).
  Incomplete runs never exit 0. Drops straight into GitHub Actions.

## Install

**Python CLI (recommended):**

```bash
pip install evaldiff
evaldiff --version
```

npm (placeholder + future GitHub Action):

```bash
npm i -g @evaldiff/evaldiff
```

## Quickstart

```bash
# 1) Get a key (self-serve account; shown once)
evaldiff signup --email you@example.com
export EVALDIFF_API_KEY=eval_...

# 2) Scaffold a dataset with two example cases
evaldiff init --name my-dataset

# 3) Gate it: upload, run, poll to completion, exit 0/1/2
#    NOTE: each `--dataset FILE` upload creates a NEW dataset — remember the
#    printed dataset id so the next run can target the same one.
evaldiff run --dataset my-dataset.json \
             --endpoint https://your-openai-compatible/v1 \
             --model your-model \
             --model-key $YOUR_MODEL_KEY \
             --threshold 0.85
# → "uploaded dataset my-dataset (2 cases) → id 42"

# 4) Gate a change against the SAME dataset (this keeps runs comparable)
evaldiff run --dataset-id 42 \
             --endpoint https://your-openai-compatible/v1 \
             --model your-model \
             --model-key $YOUR_MODEL_KEY \
             --threshold 0.85

# 5) Compare the two runs; exit 1 on any regression
#    (diff rejects runs on different datasets)
evaldiff diff --base <runA-id> --new <runB-id>

# Reports with per-case evidence (input / expected / actual / judge)
evaldiff run ... --report report --format both   # report.json + report.md
```

Env: `EVALDIFF_API_KEY` (your evaldiff key), `EVALDIFF_BASE_URL`
(default `https://api.evaldiff.io`), `EVALDIFF_MODEL_API_KEY` (key for the
model endpoint, equivalent to `--model-key`).

### Gate exit codes (stable contract)

| code | meaning |
| --- | --- |
| `0` | PASS — run finished with all cases passing / no regressions |
| `1` | FAIL — run finished with failures / regressions present |
| `2` | ERROR — auth, network, deadline, run failed, incomplete run, invalid comparison |

A deadline or a failed run is an operational error (2), never a pass (0).

## Dataset format

```json
[
  {
    "input": "Refund policy for a returned item",
    "expected": "You can return any item within 30 days for a full refund.",
    "rubric": ["mentions the 30-day window", "mentions full refund"],
    "tags": ["support"]
  }
]
```

`rubric` is optional — a list of criteria a judge model scores 0/1 against.
Without it, evaldiff falls back to exact / contains matching against `expected`.

## API (v0.0.2)

The core is a small FastAPI service (`pip install evaldiff` includes it):

```bash
evaldiff-server   # uvicorn on :8000, SQLite by default
```

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/auth/signup` | Self-serve account + API key (`eval_…`), 1k free cases/month |
| `POST /v1/datasets` | Create a dataset from inline cases |
| `GET  /v1/datasets/{id}` | Dataset detail |
| `POST /v1/runs` | Score a dataset against an OpenAI-compatible endpoint (async job) |
| `GET  /v1/runs/{id}` | Run status: `queued → running → done/failed` |
| `GET  /v1/runs/{id}/cases` | Per-case output, score, pass/fail, tokens, error |
| `GET  /v1/datasets/{id}/cases` | Dataset cases (input/expected/rubric) for reports |
| `GET  /v1/runs/{id}/cases/{seq}/output` | One case's evidence: actual output + judge verdict |
| `GET  /v1/runs/{id}/diff?compare={other_id}` | Regression diff (JSON) |
| `GET  /v1/runs/{id}/report.md?compare={other_id}` | The same as a markdown report |
| `GET  /v1/usage` | Quota / usage for the key |

Auth is a bearer `eval_…` key on every `/v1/*` call. Quota is per-account,
per calendar month; failed runs are free.

## Supported endpoints (v0)

Any OpenAI-compatible `/chat/completions` endpoint (BYO key): OpenAI,
Groq, Together, Ollama, vLLM, LM Studio. Everything else: bring your own adapter.

## Status

- `0.0.22` — fixes: backups run twice daily (03:00 + 15:00 UTC) so the 24h
  RPO holds end-to-end including capture/verification slack; OPERATIONS.md
  retention now matches backup.py exactly (all points 7d, daily 30d, weekly
  84d); CLI `--deadline` now constrains in-flight requests, not only the
  poll loop — a stalled status call can no longer run past the deadline
- `0.0.21` — fixes: reports work with normal datasets (API assigns `seq`;
  CLI falls back to list position), file-based workflow stays comparable
  (quickstart + hints for `--dataset-id`), creation requests are no longer
  auto-retried on 5xx/transport errors (no duplicate runs/quota), OPERATIONS.md
  RPO aligned to the shipped daily backup schedule
- `0.0.20` — CLI gate: `evaldiff run` + `evaldiff diff` with stable exit codes
  (0/1/2), per-case evidence endpoints, JSON/Markdown reports, LocalStorage
  for evidence without S3
- `0.0.19` — bounded responses, secret-key enforcement, deadlines + fair queueing
- Next: GitHub Action (`@evaldiff/action`), web diff view, usage-based billing


## Production credentials

Local development defaults to plaintext model-credential storage unless
`EVALDIFF_SECRET_KEY` is configured. Set `EVALDIFF_PRODUCTION=true` in production;
startup then requires a valid Fernet key and encrypted, readable stored
credentials. The bundled Compose deployment enforces this mode.

Follow [the upgrade guide](deploy/UPGRADING.md#production-credential-encryption-part-b)
for key provisioning, the offline `python -m api.migrate_secrets` command,
backup restoration, and rotation. The deployment image builds the checked-out
source; use the full repository checkout when deploying.

### Running tests from source

Install the test dependencies before running the suite:

```sh
pip install -e '.[test]'
pytest -q
```

For PostgreSQL coverage, also install `psycopg[binary]>=3.1` and set
`EVALDIFF_TEST_POSTGRES_URL` to a disposable PostgreSQL database. See
[the qualification runbook](deploy/qualification/README.md) for the isolated
PostgreSQL runner and staging scenarios.
