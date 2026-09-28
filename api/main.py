"""FastAPI app for evaldiff: datasets, runs, diff, report, usage, auth."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import __version__
from .auth import create_key, get_current_account
from .db import Base, get_session, make_engine, state
from .diff import compute_diff, diff_to_markdown
from .models import Account, Dataset, Run
from .settings import Settings
from .storage import build_storage
from .worker import enqueue_run, worker_loop


# ---------- schemas ----------
class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class SignupOut(BaseModel):
    key: str
    email: str
    quota: int


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cases: list[dict[str, Any]] = Field(min_length=1)


class DatasetOut(BaseModel):
    id: int
    name: str
    case_count: int
    created_at: datetime


class RunIn(BaseModel):
    dataset_id: int
    model: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=512)
    api_key: str = ""
    prompt_template: str = "{{ input }}"
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class RunOut(BaseModel):
    id: int
    status: str
    model: str
    total_cases: int
    passed_cases: int
    avg_score: float | None
    error: str | None
    created_at: datetime


class RunCaseOut(BaseModel):
    seq: int
    score: float | None
    passed: bool | None
    latency_ms: int | None
    tokens_in: int | None
    tokens_out: int | None
    error: str | None


class UsageOut(BaseModel):
    period: str
    quota: int
    used: int
    remaining: int


def _run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        status=run.status,
        model=run.model,
        total_cases=run.total_cases,
        passed_cases=run.passed_cases,
        avg_score=run.avg_score,
        error=run.error,
        created_at=run.created_at,
    )


# ---------- app factory ----------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="evaldiff API", version=__version__)

    @app.on_event("startup")
    def _startup() -> None:
        from sqlalchemy.orm import sessionmaker

        state.settings = settings
        state.storage = build_storage(settings)
        engine = make_engine(settings.database_url)
        state.session_factory = sessionmaker(bind=engine)
        Base.metadata.create_all(engine)
        if settings.enable_worker:
            stop = asyncio.Event()
            app.state.worker_stop = stop
            app.state.worker_task = asyncio.create_task(worker_loop(stop, interval=1.0))

    @app.on_event("shutdown")
    def _shutdown() -> None:
        with contextlib.suppress(Exception):
            app.state.worker_stop.set()
            app.state.worker_task.cancel()

    # ---------- meta ----------
    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "storage": type(state.storage).__name__,
        }

    # ---------- auth ----------
    @app.post("/v1/auth/signup", response_model=SignupOut, status_code=status.HTTP_201_CREATED)
    def signup(body: SignupIn, session: Session = Depends(get_session)) -> SignupOut:
        email = body.email.strip().lower()
        account = session.query(Account).filter(Account.email == email).first()
        if account is None:
            account = Account(email=email, monthly_quota=settings.default_quota)
            session.add(account)
            session.commit()
            session.refresh(account)
        key, _ = create_key(session, account, label="default")
        return SignupOut(key=key, email=email, quota=account.monthly_quota)

    # ---------- datasets ----------
    @app.post("/v1/datasets", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
    def create_dataset(
        body: DatasetIn,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> DatasetOut:
        if len(body.cases) > settings.max_cases_per_dataset:
            raise HTTPException(413, f"max {settings.max_cases_per_dataset} cases")
        payload = json.dumps(body.cases, ensure_ascii=False)
        if len(payload.encode()) > settings.max_dataset_bytes:
            raise HTTPException(413, "dataset too large")
        for i, c in enumerate(body.cases):
            if "input" not in c or "expected" not in c:
                raise HTTPException(422, f"case {i}: each case needs 'input' and 'expected'")
        storage_key = None
        cases_json = payload
        if settings.s3_endpoint and state.storage is not None:
            storage_key = f"datasets/{account.id}/" + _new_ds_key()
            state.storage.put_json(storage_key, body.cases)
            cases_json = None
        ds = Dataset(
            account_id=account.id,
            name=body.name,
            case_count=len(body.cases),
            storage_key=storage_key,
            cases_json=cases_json,
        )
        session.add(ds)
        session.commit()
        session.refresh(ds)
        return DatasetOut(
            id=ds.id, name=ds.name, case_count=ds.case_count, created_at=ds.created_at
        )

    @app.get("/v1/datasets", response_model=list[DatasetOut])
    def list_datasets(
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> list[DatasetOut]:
        rows = (
            session.query(Dataset)
            .filter(Dataset.account_id == account.id)
            .order_by(Dataset.id.desc())
            .all()
        )
        return [
            DatasetOut(id=d.id, name=d.name, case_count=d.case_count, created_at=d.created_at)
            for d in rows
        ]

    @app.get("/v1/datasets/{ds_id}", response_model=DatasetOut)
    def get_dataset(
        ds_id: int,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> DatasetOut:
        ds = session.get(Dataset, ds_id)
        if ds is None or ds.account_id != account.id:
            raise HTTPException(404, "dataset not found")
        return DatasetOut(
            id=ds.id, name=ds.name, case_count=ds.case_count, created_at=ds.created_at
        )

    # ---------- runs ----------
    @app.post("/v1/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
    def create_run(
        body: RunIn,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> RunOut:
        ds = session.get(Dataset, body.dataset_id)
        if ds is None or ds.account_id != account.id:
            raise HTTPException(404, "dataset not found")
        period = time.strftime("%Y-%m")
        if account.quota_period != period:
            account.quota_period = period
            account.used_cases = 0
        if account.used_cases + ds.case_count > account.monthly_quota:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "monthly case quota exceeded",
                headers={"Retry-After": "86400"},
            )
        run = Run(
            account_id=account.id,
            dataset_id=ds.id,
            model=body.model,
            endpoint=body.endpoint,
            api_key_ref=body.api_key,
            prompt_template=body.prompt_template,
            threshold=body.threshold,
            status="queued",
            total_cases=ds.case_count,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        enqueue_run(session, run.id)
        return _run_out(run)

    @app.get("/v1/runs", response_model=list[RunOut])
    def list_runs(
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> list[RunOut]:
        rows = session.query(Run).filter(Run.account_id == account.id).order_by(Run.id.desc()).all()
        return [_run_out(r) for r in rows]

    @app.get("/v1/runs/{run_id}", response_model=RunOut)
    def get_run(
        run_id: int,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> RunOut:
        run = _get_own_run(session, run_id, account)
        return _run_out(run)

    @app.get("/v1/runs/{run_id}/cases", response_model=list[RunCaseOut])
    def get_run_cases(
        run_id: int,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> list[RunCaseOut]:
        run = _get_own_run(session, run_id, account)
        return [
            RunCaseOut(
                seq=c.seq,
                score=c.score,
                passed=c.passed,
                latency_ms=c.latency_ms,
                tokens_in=c.tokens_in,
                tokens_out=c.tokens_out,
                error=c.error,
            )
            for c in sorted(run.cases, key=lambda c: c.seq)
        ]

    # ---------- diff + report ----------
    @app.get("/v1/runs/{run_id}/diff")
    def diff_runs(
        run_id: int,
        compare: int,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> dict:
        run_b = _get_own_run(session, run_id, account)
        run_a = _get_own_run(session, compare, account)
        d = compute_diff(session, run_a, run_b)
        d.cases.sort(key=lambda c: c.seq)
        return {
            "summary": d.summary,
            "cases": [c.__dict__ for c in d.cases],
            "regressions": [c.__dict__ for c in d.regressions],
        }

    @app.get("/v1/runs/{run_id}/report.md")
    def report(
        run_id: int,
        compare: int,
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> Response:
        run_b = _get_own_run(session, run_id, account)
        run_a = _get_own_run(session, compare, account)
        md = diff_to_markdown(compute_diff(session, run_a, run_b))
        return Response(content=md, media_type="text/markdown")

    # ---------- usage ----------
    @app.get("/v1/usage", response_model=UsageOut)
    def usage(
        account: Account = Depends(get_current_account),
        session: Session = Depends(get_session),
    ) -> UsageOut:
        period = time.strftime("%Y-%m")
        used = account.used_cases if account.quota_period == period else 0
        return UsageOut(
            period=period,
            quota=account.monthly_quota,
            used=used,
            remaining=max(0, account.monthly_quota - used),
        )

    return app


def _get_own_run(session: Session, run_id: int, account: Account) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.account_id != account.id:
        raise HTTPException(404, "run not found")
    return run


def _new_ds_key() -> str:
    import secrets

    return f"ds_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def run_server() -> None:
    """Entry point: `evaldiff-server` — run the API with uvicorn."""
    import uvicorn

    from .settings import Settings

    settings = Settings()
    uvicorn.run("api.main:create_app", factory=True, host="0.0.0.0", port=8000, log_level="info")
    del settings


app = None  # set lazily by the CLI/uvicorn entry point
