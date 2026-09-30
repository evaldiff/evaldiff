"""Diff logic: run B vs run A, per-case deltas, regressions highlighted."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import Run


@dataclass
class CaseDiff:
    seq: int
    a_score: float | None
    b_score: float | None
    a_passed: bool | None
    b_passed: bool | None
    delta: float | None
    regression: bool
    note: str = ""


@dataclass
class RunDiff:
    a: int
    b: int
    a_avg: float | None
    b_avg: float | None
    cases: list[CaseDiff] = field(default_factory=list)
    regressions: list[CaseDiff] = field(default_factory=list)
    improvements: list[CaseDiff] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "a_avg": self.a_avg,
            "b_avg": self.b_avg,
            "total_cases": len(self.cases),
            "regressions": len(self.regressions),
            "improvements": len(self.improvements),
            "net_delta": round((self.b_avg or 0) - (self.a_avg or 0), 4)
            if (self.a_avg is not None and self.b_avg is not None)
            else None,
        }


def compute_diff(session: Session, a: Run, b: Run, regression_drop: float = 0.1) -> RunDiff:
    a_map = {c.seq: c for c in a.cases}
    b_map = {c.seq: c for c in b.cases}
    out = RunDiff(a=a.id, b=b.id, a_avg=a.avg_score, b_avg=b.avg_score)
    for seq in sorted(set(a_map) | set(b_map)):
        ca, cb = a_map.get(seq), b_map.get(seq)
        a_score = ca.score if ca else None
        b_score = cb.score if cb else None
        a_passed = ca.passed if ca else None
        b_passed = cb.passed if cb else None
        delta = (
            round(b_score - a_score, 4) if (a_score is not None and b_score is not None) else None
        )
        regression = False
        note = ""
        if a_passed is True and b_passed is False:
            regression, note = True, "new failure"
        elif a_score is not None and b_score is not None and (b_score - a_score) < -regression_drop:
            regression, note = True, f"score drop {delta:+.2f}"
        if regression:
            cd = CaseDiff(seq, a_score, b_score, a_passed, b_passed, delta, True, note)
            out.regressions.append(cd)
            out.cases.append(cd)
        else:
            cd = CaseDiff(seq, a_score, b_score, a_passed, b_passed, delta, False)
            if delta is not None and delta > regression_drop:
                out.improvements.append(cd)
            out.cases.append(cd)
    return out


def diff_to_markdown(diff: RunDiff) -> str:
    lines = [
        f"# evaldiff report — run {diff.b} vs run {diff.a}",
        "",
        f"| metric | run A ({diff.a}) | run B ({diff.b}) | delta |",
        "|---|---|---|---|",
        f"| avg score | {fmt(diff.a_avg)} | {fmt(diff.b_avg)} | {fmt_delta(diff.summary['net_delta'])} |",
        f"| cases | {len(diff.cases)} | {len(diff.cases)} | — |",
        f"| regressions | — | {len(diff.regressions)} | — |",
        f"| improvements | — | {len(diff.improvements)} | — |",
        "",
    ]
    if diff.regressions:
        lines += ["## Regressions", ""]
        for c in diff.regressions:
            lines.append(f"- case {c.seq}: {fmt(c.a_score)} → {fmt(c.b_score)} ({c.note})")
        lines.append("")
    return "\n".join(lines)


def fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def fmt_delta(v: float | None) -> str:
    return "—" if v is None else f"{v:+.3f}"


def run_to_markdown(run: Run) -> str:
    """Single-run report (no baseline)."""
    total = len(run.cases)
    passed = sum(1 for c in run.cases if c.passed)
    lines = [
        f"# evaldiff report — run {run.id} ({run.model})",
        "",
        "| metric | value |",
        "|---|---|",
        f"| avg score | {fmt(run.avg_score)} |",
        f"| cases | {total} |",
        f"| passed | {passed} |",
        f"| pass rate | {f'{passed / total:.1%}' if total else '—'} |",
        f"| status | {run.status} |",
        "",
    ]
    failures = [c for c in run.cases if not c.passed]
    if failures:
        lines += ["## Failing cases", ""]
        for c in failures[:20]:
            err = f" — {c.error}" if c.error else ""
            lines.append(f"- case {c.seq}: score {fmt(c.score)}{err}")
        if len(failures) > 20:
            lines.append(f"- … and {len(failures) - 20} more")
        lines.append("")
    return "\n".join(lines)
