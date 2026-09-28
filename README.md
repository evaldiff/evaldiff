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
- **Gate** — `evaldiff run --threshold 0.85` → exit code `0`/`1`, drops straight
  into GitHub Actions.

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
# Scaffold a dataset with two example cases
evaldiff init --name my-dataset

# Edit my-dataset.json, then run an eval against your model endpoint
evaldiff run --dataset my-dataset.json \
             --endpoint https://your-openai-compatible/v1 \
             --model your-model \
             --threshold 0.85

# Compare two runs and see regressions
evaldiff diff <run-a-id> <run-b-id>
```

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
| `GET  /v1/runs/{id}/diff?compare={other_id}` | Regression diff (JSON) |
| `GET  /v1/runs/{id}/report.md?compare={other_id}` | The same as a markdown report |
| `GET  /v1/usage` | Quota / usage for the key |

Auth is a bearer `eval_…` key on every `/v1/*` call. Quota is per-account,
per calendar month; failed runs are free.

## Supported endpoints (v0)

Any OpenAI-compatible `/chat/completions` endpoint (BYO key): OpenAI,
Groq, Together, Ollama, vLLM, LM Studio. Everything else: bring your own adapter.

## Status

- `0.0.2` — API core: auth, datasets, runs (worker + retries), judges, diff + report
- Next: GitHub Action (`@evaldiff/action`), web diff view, usage-based billing

