# Evaldiff

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

```
npm i -g @evaldiff/evaldiff
```

Quickstart

```
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
Dataset format

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

rubric is optional — a list of criteria a judge model scores 0/1 against.
Without it, evaldiff falls back to semantic / exact similarity vs. expected.

Supported endpoints (v0)
Any OpenAI-compatible /chat/completions endpoint (BYO key)
OpenAI
Anthropic
Everything else: bring your own adapter. Scope is the moat.

