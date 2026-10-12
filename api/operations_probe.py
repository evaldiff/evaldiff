"""Disposable dependency probe; input secrets arrive only on stdin.

A parent process enforces the total deadline and kills this process if database
or synchronous S3 I/O stalls. Never print exception details or input values.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.pool import NullPool

from .leases import JobLease
from .models import Job, Run, RunCase


def probe(config: dict) -> dict:
    result = {"database": False, "storage": False, "metrics": {}}
    engine = None
    try:
        engine = create_engine(config["database_url"], poolclass=NullPool, hide_parameters=True)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recent = now - timedelta(minutes=5)
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET statement_timeout = '2000ms'"))
            connection.execute(text("SELECT 1"))
            pending, oldest = connection.execute(
                select(func.count(), func.min(Job.created_at)).where(Job.status == "pending")
            ).one()
            metrics = {
                "queue_depth": pending,
                "queue_oldest_seconds": max(0, (now - oldest).total_seconds()) if oldest else 0,
                "active_jobs": connection.scalar(
                    select(func.count()).select_from(Job).where(Job.status == "running")
                ),
                "expired_leases": connection.scalar(
                    select(func.count())
                    .select_from(Job)
                    .join(JobLease)
                    .where(
                        Job.status == "running",
                        or_(JobLease.job_id.is_(None), JobLease.expires_at <= now),
                    )
                ),
                "failed_runs_5m": connection.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(Run.status == "failed", Run.finished_at >= recent)
                ),
                "timed_out_runs_5m": connection.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.status == "failed",
                        Run.finished_at >= recent,
                        Run.error.like("RunDeadlineExceeded:%"),
                    )
                ),
                "case_errors_5m": connection.scalar(
                    select(func.count())
                    .select_from(RunCase)
                    .join(Run)
                    .where(Run.finished_at >= recent, RunCase.error.is_not(None))
                ),
            }
            result.update(database=True, metrics=metrics)
    except Exception:  # noqa: BLE001 - exceptions can contain credentials
        result["database"] = False
    finally:
        if engine is not None:
            engine.dispose()
    if not config.get("s3_endpoint"):
        result["storage"] = result["database"]  # dataset storage is the database
    else:
        try:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=config["s3_endpoint"],
                aws_access_key_id=config["s3_access_key"],
                aws_secret_access_key=config["s3_secret_key"],
                region_name=config["s3_region"],
                config=Config(
                    connect_timeout=1,
                    read_timeout=1,
                    retries={"total_max_attempts": 1},
                    signature_version="s3v4",
                ),
            )
            try:
                client.head_bucket(Bucket=config["s3_bucket"])
                key = config.get("probe_key", ".evaldiff-readiness/probe")
                client.put_object(Bucket=config["s3_bucket"], Key=key, Body=b"ok")
                try:
                    response = client.get_object(Bucket=config["s3_bucket"], Key=key)
                    try:
                        if response["Body"].read(3) != b"ok":
                            raise ValueError("readiness object mismatch")
                    finally:
                        response["Body"].close()
                finally:
                    client.delete_object(Bucket=config["s3_bucket"], Key=key)
                result["storage"] = True
            finally:
                client.close()
        except Exception:  # noqa: BLE001 - never print S3 error details
            result["storage"] = False
    return result


if __name__ == "__main__":
    try:
        output = probe(json.loads(sys.stdin.read()))
    except Exception:  # noqa: BLE001 - emit only fixed failure fields
        output = {"database": False, "storage": False, "metrics": {}}
    print(json.dumps(output))
