# @evaldiff/evaldiff

[![npm version](https://img.shields.io/npm/v/@evaldiff/evaldiff)](https://www.npmjs.com/package/@evaldiff/evaldiff)
[![PyPI version](https://img.shields.io/pypi/v/evaldiff)](https://pypi.org/project/evaldiff/)

> **CI for LLM prompts.** Datasets, eval runs, and regression diffs as a
> pass/fail gate for your CI — the `npm test` of prompts.

**This npm package is the name reservation and the future GitHub Action entry
point. The full CLI lives on PyPI:**

```bash
pip install evaldiff
evaldiff --version
```

## What this package provides today (stub)

- Installs the `evaldiff` command (name reservation)
- `evaldiff --version` → prints the version
- Any other invocation prints a short pointer to the PyPI CLI and exits `0`

```bash
npx @evaldiff/evaldiff --version
# evaldiff 0.0.23
```

## The real CLI (PyPI)

```bash
pip install evaldiff

# Gate a dataset against a model: exit 0 = pass, 1 = regression/failure,
# 2 = operational error. Incomplete runs never exit 0.
evaldiff run --dataset ds.json --endpoint https://…/v1 --model my-model \
             --model-key $KEY --threshold 0.8

# Compare two runs: regressions highlighted, side by side.
evaldiff diff --base run-a --new run-b
```

Docs & status: [https://evaldiff.io](https://evaldiff.io)

## Roadmap (this package)

- GitHub Action built on the PyPI CLI
- Native Node entrypoint for CI pipelines that prefer pure-JS tooling

## Links

- Repository: <https://github.com/evaldiff/evaldiff>
- PyPI: <https://pypi.org/project/evaldiff/>
- License: MIT
