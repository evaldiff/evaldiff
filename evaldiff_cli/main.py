"""evaldiff — CI for LLM prompts.

Commands
--------
signup    create an account; print the one-time API key
init      scaffold a dataset JSON file
run       upload dataset (or reuse an id), submit a run, poll to completion,
          gate on pass/fail — the CI gate
diff      compare two finished runs (base A vs new B); gate on regressions
status    show one run
report    fetch the server's markdown report for a run
datasets  list datasets
runs      list runs
usage     quota / usage for the key
version   print the version

Gate exit codes (stable contract for CI — documented, do not change):
  0  PASS   — run finished with ALL cases passing / no regressions
  1  FAIL   — run finished with failures / regressions present
  2  ERROR  — operational or usage error: auth, network, deadline exceeded,
              run failed, incomplete run, invalid comparison, bad arguments.
              Incomplete or failed runs and polling deadlines NEVER exit 0.
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("evaldiff")
except Exception:  # noqa: BLE001 - running from a checkout without an installed dist
    __version__ = "0.0.20"

DEFAULT_BASE_URL = "https://api.evaldiff.io"
DEFAULT_DEADLINE = 1800.0  # total poll budget for `run`
DEFAULT_POLL = 5.0
REQUEST_TIMEOUT = 30.0  # per-request
CALL_BUDGET = 120.0  # budget for single non-polling calls (upload/submit/fetch)

# Report evidence policy: always fetch details for failing/regressed cases,
# plus a bounded sample of passing cases; hard cap on per-case fetches.
REPORT_DETAIL_CASES = 20
REPORT_MAX_FETCHES = 100
REPORT_CLIP = 1000  # chars per field in markdown (JSON keeps full values)

EXIT_PASS = 0
EXIT_GATE_FAIL = 1
EXIT_ERROR = 2

app = typer.Typer(
    name="evaldiff",
    help="CI for LLM prompts: datasets, eval runs, regression diffs.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


# ---------- HTTP layer ----------
class ApiError(Exception):
    def __init__(self, status: int, detail: str, retry_after: float | None = None) -> None:
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {detail}")


class DeadlineExceeded(Exception):
    def __init__(self, note: str = "") -> None:
        self.note = note
        super().__init__(note)


class Gateway:
    """Auth'd HTTP client over the evaldiff API. Thin on purpose so tests
    can swap the transport (httpx.MockTransport) or the factory."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers or {},
            timeout=timeout,
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        resp = self._client.request(method, path, params=params, json=json_body)
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detail(resp), _retry_after(resp))
        return resp

    def close(self) -> None:
        self._client.close()


def _gateway(base_url: str | None, key: str | None, timeout: float = REQUEST_TIMEOUT) -> Gateway:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return Gateway(base, headers=headers, timeout=timeout)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON error bodies
        return resp.text[:200] or f"{resp.status_code}"
    if isinstance(body, dict):
        d = body.get("detail") or body.get("error") or body
        if isinstance(d, dict):
            d = json.dumps(d)[:200]
        return str(d)[:300]
    return str(body)[:300]


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _sleep_bounded(seconds: float, deadline: float) -> None:
    wait = max(0.0, min(seconds, deadline - time.monotonic()))
    if wait > 0:
        time.sleep(wait)


def call(
    gw: Gateway,
    method: str,
    path: str,
    *,
    deadline: float,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    backoff_start: float = 1.0,
    backoff_max: float = 15.0,
) -> httpx.Response:
    """One logical API call, retried within ``deadline``.

    429 → honor Retry-After (capped by the deadline); 5xx and transport
    errors → exponential backoff; every other 4xx → terminal (auth,
    validation, quota-at-enqueue, 409 comparison conflicts...). Never
    loops past the deadline, never turns an error into success.
    """
    wait = backoff_start
    while True:
        if time.monotonic() >= deadline:
            raise DeadlineExceeded(path)
        try:
            return gw.request(method, path, params=params, json_body=json_body)
        except ApiError as e:
            if e.status == 429:
                wait = max(wait, min(e.retry_after or wait, backoff_max))
            elif not (500 <= e.status < 600):
                raise
        except httpx.TransportError:
            pass
        _sleep_bounded(wait, deadline)
        wait = min(wait * 2, backoff_max)


def _auth_err(e: ApiError) -> str:
    if e.status in (401, 403):
        return (
            f"auth failed (HTTP {e.status}: {e.detail}) — check --key / "
            "EVALDIFF_API_KEY; create a key with `evaldiff signup --email you@example.com`"
        )
    return f"HTTP {e.status}: {e.detail}"


def _fail(msg: str) -> typer.Exit:
    err_console.print(f"[bold red]error:[/bold red] {msg}")
    return typer.Exit(EXIT_ERROR)


def _fmt(v: Any) -> str:
    return "—" if v is None else (f"{v:.3f}" if isinstance(v, (int, float)) else str(v))


def _clip(text: Any, limit: int = REPORT_CLIP) -> str | None:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[:limit] + f" …[+{len(s) - limit} chars]"


# ---------- dataset + run helpers ----------
def _read_dataset_file(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        cases = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise _fail(f"{path} is not valid JSON: {e}")
    if not isinstance(cases, list) or not cases:
        raise _fail(f"{path} must be a non-empty JSON array of cases")
    for i, c in enumerate(cases):
        if not isinstance(c, dict) or "input" not in c or "expected" not in c:
            raise _fail(f"{path} case {i}: each case needs 'input' and 'expected'")
    return cases


def _upload_dataset(gw: Gateway, path: pathlib.Path, name: str) -> int:
    cases = _read_dataset_file(path)
    resp = call(
        gw,
        "POST",
        "/v1/datasets",
        deadline=time.monotonic() + CALL_BUDGET,
        json_body={"name": name, "cases": cases},
    )
    console.print(
        f"uploaded dataset [bold]{name}[/bold] ({len(cases)} cases) → id {resp.json()['id']}"
    )
    return resp.json()["id"]


def _submit_run(
    gw: Gateway,
    *,
    dataset_id: int,
    model: str,
    endpoint: str,
    model_key: str | None,
    template: str,
    threshold: float,
) -> dict[str, Any]:
    body = {
        "dataset_id": dataset_id,
        "model": model,
        "endpoint": endpoint,
        "api_key": model_key or "",
        "prompt_template": template,
        "threshold": threshold,
    }
    resp = call(gw, "POST", "/v1/runs", deadline=time.monotonic() + CALL_BUDGET, json_body=body)
    return resp.json()


def _poll_run(gw: Gateway, run_id: int, deadline: float, poll: float) -> tuple[dict[str, Any], str]:
    """Poll until terminal. Returns (run_out, 'pass'|'fail'|'error').

    'error' = the run itself failed server-side (exit 2); deadline expiry
    raises DeadlineExceeded (exit 2). Only 'done' can produce 0 or 1.
    """
    started = time.monotonic()
    while True:
        out = call(gw, "GET", f"/v1/runs/{run_id}", deadline=deadline).json()
        st = out.get("status")
        if st == "done":
            passed, total = out.get("passed_cases"), out.get("total_cases")
            ok = passed is not None and total is not None and passed == total
            return out, ("pass" if ok else "fail")
        if st == "failed":
            return out, "error"
        console.print(f"… run {run_id} {st} (t+{int(time.monotonic() - started)}s)")
        _sleep_bounded(poll, deadline)


# ---------- reports (Part E: input / expected / actual / judge) ----------
def _fetch_dataset_cases(gw: Gateway, ds_id: int) -> dict[str, Any]:
    return call(
        gw, "GET", f"/v1/datasets/{ds_id}/cases", deadline=time.monotonic() + CALL_BUDGET
    ).json()


def _case_detail(gw: Gateway, run_id: int, seq: int) -> dict[str, Any] | None:
    """No evidence (404: not persisted) is None; storage outage (502) too —
    the report degrades, it never fabricates a pass."""
    try:
        return call(
            gw,
            "GET",
            f"/v1/runs/{run_id}/cases/{seq}/output",
            deadline=time.monotonic() + CALL_BUDGET,
        ).json()
    except ApiError as e:
        if e.status in (404, 502):
            return None
        raise


def _build_run_report(gw: Gateway, run_out: dict[str, Any]) -> dict[str, Any]:
    ds = _fetch_dataset_cases(gw, run_out["dataset_id"])
    cases = call(
        gw, "GET", f"/v1/runs/{run_out['id']}/cases", deadline=time.monotonic() + CALL_BUDGET
    ).json()
    by_seq = {c["seq"]: c for c in ds.get("cases", [])}
    failed_seqs = [c["seq"] for c in cases if c.get("passed") is not True]
    pass_seqs = [c["seq"] for c in cases if c.get("passed") is True][:REPORT_DETAIL_CASES]
    detail_seqs = (failed_seqs + pass_seqs)[:REPORT_MAX_FETCHES]
    details = {s: d for s in detail_seqs if (d := _case_detail(gw, run_out["id"], s))}
    rows = []
    for c in cases:
        src = by_seq.get(c["seq"], {})
        d = details.get(c["seq"]) or {}
        sc = d.get("score") or {}
        rows.append(
            {
                "seq": c["seq"],
                "input": src.get("input"),
                "expected": src.get("expected"),
                "rubric": src.get("rubric"),
                "score": c.get("score"),
                "passed": c.get("passed"),
                "error": c.get("error"),
                "latency_ms": c.get("latency_ms"),
                "tokens_in": c.get("tokens_in"),
                "tokens_out": c.get("tokens_out"),
                "actual_output": d.get("output"),
                "judge_detail": sc.get("detail"),
                "judge_raw": sc.get("raw"),
            }
        )
    return {
        "kind": "run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": run_out,
        "dataset": ds,
        "cases": rows,
    }


def _build_diff_report(
    gw: Gateway, base_out: dict[str, Any], new_out: dict[str, Any], diff_out: dict[str, Any]
) -> dict[str, Any]:
    ds = _fetch_dataset_cases(gw, new_out["dataset_id"])
    by_seq = {c["seq"]: c for c in ds.get("cases", [])}
    reg_seqs = [c["seq"] for c in diff_out.get("regressions", [])]
    other_seqs = [c["seq"] for c in diff_out.get("cases", []) if c["seq"] not in reg_seqs][
        :REPORT_DETAIL_CASES
    ]
    fetch_b = (reg_seqs + other_seqs)[:REPORT_MAX_FETCHES]
    details_b = {s: d for s in fetch_b if (d := _case_detail(gw, new_out["id"], s))}
    details_a = {
        s: d for s in reg_seqs[:REPORT_MAX_FETCHES] if (d := _case_detail(gw, base_out["id"], s))
    }
    rows = []
    for c in diff_out.get("cases", []):
        src = by_seq.get(c["seq"], {})
        db = details_b.get(c["seq"]) or {}
        da = details_a.get(c["seq"]) or {}
        scb = db.get("score") or {}
        rows.append(
            {
                "seq": c["seq"],
                "regression": c.get("regression"),
                "note": c.get("note"),
                "input": src.get("input"),
                "expected": src.get("expected"),
                "rubric": src.get("rubric"),
                "base_score": c.get("a_score"),
                "new_score": c.get("b_score"),
                "delta": c.get("delta"),
                "base_output": da.get("output"),
                "new_output": db.get("output"),
                "new_judge_detail": scb.get("detail"),
                "new_judge_raw": scb.get("raw"),
            }
        )
    return {
        "kind": "diff",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base_out,
        "new": new_out,
        "summary": diff_out.get("summary"),
        "dataset": ds,
        "cases": rows,
    }


def _render_run_md(rep: dict[str, Any]) -> str:
    run = rep["run"]
    passed, total = run.get("passed_cases"), run.get("total_cases")
    gate = "PASS" if (passed is not None and total is not None and passed == total) else "FAIL"
    lines = [
        f"# evaldiff report — run {run.get('id')} ({run.get('model')})",
        "",
        "| metric | value |",
        "|---|---|",
        f"| status | {run.get('status')} |",
        f"| cases | {total} |",
        f"| passed | {passed} |",
        f"| avg score | {_fmt(run.get('avg_score'))} |",
        f"| threshold | {_fmt(run.get('threshold'))} |",
        f"| GATE | **{gate}** |",
        "",
        "## Cases",
        "",
    ]
    for r in rep["cases"]:
        verdict = "PASS" if r["passed"] else "FAIL"
        lines.append(f"### case {r['seq']} — {verdict} (score {_fmt(r['score'])})")
        if r.get("input") is not None:
            lines.append(f"- **input:** {_clip(r['input'])}")
        if r.get("expected") is not None:
            lines.append(f"- **expected:** {_clip(r['expected'])}")
        if r.get("actual_output") is not None:
            lines.append(f"- **actual:** {_clip(r['actual_output'])}")
        if r.get("judge_detail"):
            lines.append(f"- **judge:** {r['judge_detail']}")
        if r.get("error"):
            lines.append(f"- **error:** {r['error']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_diff_md(rep: dict[str, Any]) -> str:
    s = rep.get("summary") or {}
    base, new = rep["base"], rep["new"]
    gate = "PASS" if not s.get("regressions") else "FAIL"
    lines = [
        f"# evaldiff diff — new run {new.get('id')} vs base run {base.get('id')}",
        "",
        "| metric | base | new | delta |",
        "|---|---|---|---|",
        f"| avg score | {_fmt(s.get('a_avg'))} | {_fmt(s.get('b_avg'))} | {_fmt(s.get('net_delta'))} |",
        f"| cases | {s.get('total_cases')} | {s.get('total_cases')} | — |",
        f"| regressions | — | {s.get('regressions')} | — |",
        f"| improvements | — | {s.get('improvements')} | — |",
        f"| GATE | — | **{gate}** | — |",
        "",
    ]
    regs = [r for r in rep["cases"] if r.get("regression")]
    if regs:
        lines += ["## Regressions", ""]
        for r in regs:
            lines.append(f"### case {r['seq']} — {r.get('note') or 'regression'}")
            if r.get("input") is not None:
                lines.append(f"- **input:** {_clip(r['input'])}")
            if r.get("expected") is not None:
                lines.append(f"- **expected:** {_clip(r['expected'])}")
            lines.append(
                f"- **base score:** {_fmt(r.get('base_score'))} → **new score:** {_fmt(r.get('new_score'))}"
            )
            if r.get("base_output") is not None:
                lines.append(f"- **base output:** {_clip(r['base_output'])}")
            if r.get("new_output") is not None:
                lines.append(f"- **new output:** {_clip(r['new_output'])}")
            if r.get("new_judge_detail"):
                lines.append(f"- **judge:** {r['new_judge_detail']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_reports(rep: dict[str, Any], path: pathlib.Path, fmt: str) -> list[pathlib.Path]:
    written: list[pathlib.Path] = []
    if fmt in ("json", "both"):
        p = path if path.suffix == ".json" else path.with_name(path.name + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
        written.append(p)
    if fmt in ("md", "both"):
        p = path if path.suffix == ".md" else path.with_name(path.name + ".md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_render_run_md(rep) if rep.get("kind") == "run" else _render_diff_md(rep))
        written.append(p)
    return written


# ---------- commands ----------
@app.command()
def version() -> None:
    """Print the evaldiff version."""
    console.print(f"evaldiff {__version__}")


@app.command()
def init(
    name: str = typer.Option("my-dataset", help="Dataset name"),
) -> None:
    """Scaffold a dataset JSON file in the current directory."""
    path = pathlib.Path(f"{name}.json")
    if path.exists():
        console.print(f"[yellow]{path} already exists, leaving untouched.[/yellow]")
        raise typer.Exit(0)
    sample = [
        {
            "input": "Refund policy for a returned item",
            "expected": "You can return any item within 30 days for a full refund.",
            "rubric": ["mentions the 30-day window", "mentions full refund"],
            "tags": ["support"],
        },
        {
            "input": "How do I reset my password?",
            "expected": "Go to Settings → Security → Reset password and follow the email link.",
            "rubric": ["mentions Settings", "mentions Security"],
            "tags": ["support"],
        },
    ]
    path.write_text(json.dumps(sample, indent=2) + "\n")
    console.print(f"[green]Wrote[/green] {path} with {len(sample)} example cases.")
    console.print(
        "Edit it, then gate it: [bold]evaldiff run --dataset "
        f"{path.name} --endpoint <url> --model <name>[/bold]"
    )


@app.command()
def signup(
    email: str = typer.Option(..., "--email", help="Account email"),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
) -> None:
    """Create an account and print the one-time API key."""
    gw = _gateway(base_url, None)
    try:
        resp = call(
            gw,
            "POST",
            "/v1/auth/signup",
            deadline=time.monotonic() + CALL_BUDGET,
            json_body={"email": email},
        )
    except ApiError as e:
        if e.status == 409:
            raise _fail(
                f"an account for {email} already exists — use your existing EVALDIFF_API_KEY"
            )
        raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    body = resp.json()
    console.print(
        f"[green]Created account[/green] {body.get('email')} (quota {body.get('quota')} cases/month)"
    )
    console.print(f"API key: [bold]{body.get('key')}[/bold]")
    console.print("Store it securely — e.g. [bold]export EVALDIFF_API_KEY=…[/bold]")


@app.command()
def run(
    dataset: pathlib.Path | None = typer.Option(
        None,
        "--dataset",
        exists=True,
        dir_okay=False,
        help="Dataset JSON file (array of cases) to upload",
    ),
    dataset_id: int | None = typer.Option(None, "--dataset-id", help="Use an existing dataset id"),
    name: str = typer.Option("cli-dataset", "--name", help="Dataset name when uploading a file"),
    endpoint: str = typer.Option(..., "--endpoint", help="OpenAI-compatible endpoint (…/v1)"),
    model: str = typer.Option(..., "--model", help="Model name for the endpoint"),
    model_key: str | None = typer.Option(
        None,
        "--model-key",
        envvar="EVALDIFF_MODEL_API_KEY",
        help="API key FOR THE MODEL endpoint (not the evaldiff key)",
    ),
    template: str = typer.Option("{{ input }}", "--template", help="Prompt template"),
    threshold: float = typer.Option(
        0.8, "--threshold", min=0.0, max=1.0, help="Per-case pass threshold (score >= threshold)"
    ),
    deadline: float = typer.Option(
        DEFAULT_DEADLINE,
        "--deadline",
        min=1.0,
        help="Max seconds to wait for the run to finish (poll budget)",
    ),
    poll: float = typer.Option(DEFAULT_POLL, "--poll", min=0.5, help="Poll interval seconds"),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Submit and return immediately (NOT a gate)"
    ),
    report: pathlib.Path | None = typer.Option(
        None, "--report", help="Write a report here (.json / .md / both by --format)"
    ),
    fmt: str = typer.Option("both", "--format", help="Report format: md | json | both"),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """Run an eval: upload dataset (optional), submit, poll, gate.

    Exit 0 = all cases passed, 1 = some failed, 2 = operational error.
    """
    if not key:
        raise _fail(
            "no API key — set EVALDIFF_API_KEY or pass --key "
            "(create one with `evaldiff signup --email you@example.com`)"
        )
    if bool(dataset) == bool(dataset_id):
        raise _fail("provide exactly one of --dataset FILE or --dataset-id N")
    gw = _gateway(base_url, key)
    run_id = None
    try:
        if dataset is not None:
            ds_id = _upload_dataset(gw, dataset, name)
        else:
            assert dataset_id is not None  # validated above
            ds_id = dataset_id
        run_out = _submit_run(
            gw,
            dataset_id=ds_id,
            model=model,
            endpoint=endpoint,
            model_key=model_key,
            template=template,
            threshold=threshold,
        )
        run_id = run_out["id"]
        console.print(f"submitted run [bold]{run_id}[/bold] (status {run_out.get('status')})")
        if no_wait:
            console.print(
                f"not waiting — check later: `evaldiff status --run {run_id}` "
                "(this did NOT gate; the run may still be in flight)"
            )
            raise typer.Exit(EXIT_PASS)
        out, outcome = _poll_run(gw, run_id, time.monotonic() + deadline, poll)
        if outcome == "pass":
            console.print(
                f"[bold green]PASS[/bold green] run {run_id}: "
                f"{out.get('passed_cases')}/{out.get('total_cases')} cases "
                f"(avg {_fmt(out.get('avg_score'))})"
            )
        elif outcome == "fail":
            console.print(
                f"[bold red]FAIL[/bold red] run {run_id}: "
                f"{out.get('passed_cases')}/{out.get('total_cases')} cases "
                f"(avg {_fmt(out.get('avg_score'))}) — "
                f"`evaldiff report --run {run_id}` for details"
            )
        else:
            raise _fail(f"run {run_id} failed server-side: {out.get('error') or 'unknown error'}")
        if report:
            try:
                rep = _build_run_report(gw, out)
                written = _write_reports(rep, report, fmt)
                for p in written:
                    console.print(f"wrote {p}")
            except ApiError as e:
                console.print(f"[yellow]warning: report incomplete ({_auth_err(e)})[/yellow]")
            except (OSError, json.JSONDecodeError) as e:
                console.print(f"[yellow]warning: could not write report ({e})[/yellow]")
        raise typer.Exit(EXIT_PASS if outcome == "pass" else EXIT_GATE_FAIL)
    except ApiError as e:
        raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    except DeadlineExceeded:
        raise _fail(
            f"deadline exceeded — run {run_id} may still finish server-side; "
            f"check `evaldiff status --run {run_id}` (a timeout is never a pass)"
        )
    finally:
        gw.close()


@app.command()
def diff(
    base: int = typer.Option(..., "--base", help="Baseline (A) run id"),
    new: int = typer.Option(..., "--new", help="New (B) run id"),
    report: pathlib.Path | None = typer.Option(None, "--report"),
    fmt: str = typer.Option("both", "--format", help="Report format: md | json | both"),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """Compare base vs new run. Exit 0 = no regressions, 1 = regressions,
    2 = incomplete run / invalid comparison / operational error."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    regs: list[dict[str, Any]] = []
    s: dict[str, Any] = {}
    try:
        try:
            base_out = call(
                gw, "GET", f"/v1/runs/{base}", deadline=time.monotonic() + CALL_BUDGET
            ).json()
            new_out = call(
                gw, "GET", f"/v1/runs/{new}", deadline=time.monotonic() + CALL_BUDGET
            ).json()
        except ApiError as e:
            raise _fail(
                _auth_err(e)
                if e.status in (401, 403)
                else f"run not found (HTTP {e.status}) — check the run id and your key"
            )
        for out in (base_out, new_out):
            if out.get("status") != "done":
                raise _fail(
                    f"run {out['id']} is '{out.get('status')}', not done — a gate is never "
                    f"green on an incomplete run; check `evaldiff status --run {out['id']}`"
                )
        try:
            diff_out = call(
                gw,
                "GET",
                f"/v1/runs/{new}/diff",
                deadline=time.monotonic() + CALL_BUDGET,
                params={"compare": base},
            ).json()
        except ApiError as e:
            raise _fail(
                f"comparison rejected (HTTP {e.status}): {e.detail} — "
                "runs must be done and use the same dataset"
            )
        regs = diff_out.get("regressions", [])
        s = diff_out.get("summary") or {}
        table = Table(title=f"diff: new run {new} vs base run {base}")
        table.add_column("case")
        table.add_column("base", justify="right")
        table.add_column("new", justify="right")
        table.add_column("delta", justify="right")
        for c in diff_out.get("cases", []):
            table.add_row(
                str(c["seq"]),
                _fmt(c.get("a_score")),
                _fmt(c.get("b_score")),
                _fmt(c.get("delta")),
                style="red" if c.get("regression") else None,
            )
        console.print(table)
        if regs:
            console.print(f"[bold red]FAIL[/bold red] {len(regs)} regression(s):")
            for c in regs:
                console.print(
                    f"  case {c['seq']}: {_fmt(c.get('a_score'))} → {_fmt(c.get('b_score'))} ({c.get('note')})"
                )
        else:
            console.print(
                f"[bold green]PASS[/bold green] no regressions "
                f"(avg {_fmt(s.get('a_avg'))} → {_fmt(s.get('b_avg'))}, "
                f"{s.get('improvements', 0)} improvements)"
            )
        if report:
            try:
                rep = _build_diff_report(gw, base_out, new_out, diff_out)
                for p in _write_reports(rep, report, fmt):
                    console.print(f"wrote {p}")
            except ApiError as e:
                console.print(
                    f"[yellow]warning: report incomplete (HTTP {e.status}: {e.detail})[/yellow]"
                )
            except (OSError, json.JSONDecodeError) as e:
                console.print(f"[yellow]warning: could not write report ({e})[/yellow]")
    finally:
        gw.close()
    raise typer.Exit(EXIT_GATE_FAIL if regs else EXIT_PASS)


@app.command()
def status(
    run_id: int = typer.Option(..., "--run", help="Run id"),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """Show one run's status (informational — not a gate)."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    try:
        try:
            out = call(
                gw, "GET", f"/v1/runs/{run_id}", deadline=time.monotonic() + CALL_BUDGET
            ).json()
        except ApiError as e:
            raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    finally:
        gw.close()
    console.print(
        f"run {out['id']}: status={out['status']} model={out.get('model')} "
        f"passed={out.get('passed_cases')}/{out.get('total_cases')} "
        f"avg={_fmt(out.get('avg_score'))}"
    )
    if out.get("error"):
        console.print(f"error: {out['error']}")
    raise typer.Exit(0)


@app.command()
def report(
    run_id: int = typer.Option(..., "--run", help="Run id"),
    compare: int | None = typer.Option(None, "--compare", help="Base run id for a diff report"),
    out: pathlib.Path | None = typer.Option(
        None, "--out", help="Write to file instead of printing"
    ),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """Fetch the server's markdown report for a run (informational)."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    try:
        try:
            resp = call(
                gw,
                "GET",
                f"/v1/runs/{run_id}/report.md",
                deadline=time.monotonic() + CALL_BUDGET,
                params={"compare": compare} if compare is not None else None,
            )
        except ApiError as e:
            raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    finally:
        gw.close()
    text = resp.text
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        console.print(f"wrote {out}")
    else:
        console.print(text, markup=False)
    raise typer.Exit(0)


@app.command()
def datasets(
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """List your datasets."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    try:
        try:
            rows = call(
                gw,
                "GET",
                "/v1/datasets",
                deadline=time.monotonic() + CALL_BUDGET,
                params={"limit": limit},
            ).json()
        except ApiError as e:
            raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    finally:
        gw.close()
    if not rows:
        console.print("no datasets yet — `evaldiff init` + `evaldiff run --dataset FILE ...`")
    for d in rows:
        console.print(f"id={d['id']}  {d.get('name')}  cases={d.get('case_count')}")
    raise typer.Exit(0)


@app.command()
def runs(
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """List your runs (newest first)."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    try:
        try:
            rows = call(
                gw,
                "GET",
                "/v1/runs",
                deadline=time.monotonic() + CALL_BUDGET,
                params={"limit": limit},
            ).json()
        except ApiError as e:
            raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    finally:
        gw.close()
    if not rows:
        console.print("no runs yet")
    for r in rows:
        console.print(
            f"id={r['id']}  status={r['status']}  model={r.get('model')}  "
            f"passed={r.get('passed_cases')}/{r.get('total_cases')}  avg={_fmt(r.get('avg_score'))}"
        )
    raise typer.Exit(0)


@app.command()
def usage(
    base_url: str = typer.Option(None, "--base-url", envvar="EVALDIFF_BASE_URL"),
    key: str | None = typer.Option(None, "--key", envvar="EVALDIFF_API_KEY"),
) -> None:
    """Show quota / usage for this key."""
    if not key:
        raise _fail("no API key — set EVALDIFF_API_KEY or pass --key")
    gw = _gateway(base_url, key)
    try:
        try:
            u = call(gw, "GET", "/v1/usage", deadline=time.monotonic() + CALL_BUDGET).json()
        except ApiError as e:
            raise _fail(_auth_err(e) if e.status in (401, 403) else f"HTTP {e.status}: {e.detail}")
    finally:
        gw.close()
    console.print(
        f"period={u.get('period')}  used={u.get('used')}  "
        f"quota={u.get('quota')}  remaining={u.get('remaining')}"
    )
    raise typer.Exit(0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
