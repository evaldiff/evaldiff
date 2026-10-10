"""Scorers. Deterministic ones need no LLM; rubric-based uses an OpenAI-compatible judge."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from .http_limits import post_json_bounded


@dataclass
class Score:
    score: float  # 0..1
    passed: bool
    raw: dict | None = None  # judge payload (stored to object storage)
    detail: str = ""


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def exact_match(output: str, expected: str) -> Score:
    same = _normalize(output) == _normalize(expected)
    return Score(
        score=1.0 if same else 0.0,
        passed=same,
        detail="exact match" if same else "not an exact match",
    )


def contains(output: str, expected: str) -> Score:
    ok = _normalize(expected) in _normalize(output)
    return Score(
        score=1.0 if ok else 0.0,
        passed=ok,
        detail=f"expected fragment {'present' if ok else 'missing'}",
    )


async def rubric_llm(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    model: str,
    api_key: str,
    output: str,
    expected: str,
    rubric: list[str],
    timeout: float = 60.0,
) -> Score:
    """Score each rubric criterion 0/1 with a judge LLM, average them.

    Strictness rules (a sloppy judge must not inflate the score):
    - ``passed`` must be a real JSON boolean — ``"false"`` (a string),
      ``0``, ``null`` or anything else counts as NOT passed.
    - Every rubric criterion must have a verdict. Missing, extra, or
      malformed entries fail their criterion; the denominator is always
      the number of rubric criteria, never the number of returned verdicts.

    Endpoint must speak OpenAI-compatible /chat/completions.
    """
    url = endpoint.rstrip("/")
    if url.endswith("/chat/completions"):
        pass
    elif url.endswith("/v1"):
        url = url + "/chat/completions"
    else:
        url = url + "/v1/chat/completions"
    prompt = (
        "You are a strict eval judge. For EACH criterion below, decide if the "
        "MODEL OUTPUT satisfies it. Answer with JSON only: "
        '{"verdicts":[{"criterion": "<criterion text>", "passed": true|false}...]}\n\n'
        f"EXPECTED ANSWER:\n{expected}\n\n"
        f"MODEL OUTPUT:\n{output}\n\n"
        "CRITERIA:\n" + "\n".join(f"- {c}" for c in rubric)
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Bounded read: same byte/idle caps as the model path (NEEDS-DOING A).
    # ResponseLimitExceeded / ResponseStalled propagate without retry and
    # the case is not charged (deterministic, not transient).
    data = await post_json_bounded(client, url, body=body, headers=headers, timeout=timeout)
    content = data["choices"][0]["message"]["content"]
    verdicts = _parse_verdicts(content)
    # Match by criterion identity, allowing reordered verdicts but never
    # counting duplicate verdicts as evidence for an unjudged criterion.
    n = len(rubric)
    by_criterion: dict[str, list[dict]] = {}
    for verdict in verdicts:
        if isinstance(verdict, dict) and isinstance(verdict.get("criterion"), str):
            by_criterion.setdefault(verdict["criterion"], []).append(verdict)
    hits = 0
    for criterion in rubric:
        matches = by_criterion.get(criterion, [])
        if len(matches) == 1 and matches[0].get("passed") is True:
            hits += 1
    score = hits / n if n else 0.0
    return Score(
        score=score,
        passed=score >= 0.5,
        raw={"verdicts": verdicts, "rubric": rubric},
        detail=f"{hits}/{n} criteria",
    )


def _parse_verdicts(content: str) -> list[dict]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{") : content.rfind("}") + 1]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    v = data.get("verdicts") if isinstance(data, dict) else None
    return v if isinstance(v, list) else []


async def score_case(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    model: str,
    api_key: str,
    output: str,
    expected: str,
    rubric: list[str],
) -> Score:
    """Route a single case to the right scorer."""
    if rubric:
        return await rubric_llm(
            client,
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            output=output,
            expected=expected,
            rubric=rubric,
        )
    if expected.strip().lower().startswith(("[[exact]]", "[exact]")):
        return exact_match(output, expected.replace("[[exact]]", "").replace("[exact]", "").strip())
    return contains(output, expected)


def judge_from_config(run) -> dict:
    """Judge = the run's own endpoint+model (BYO) in v0."""
    return {"endpoint": run.endpoint, "model": run.model, "api_key": run.api_key_ref}
