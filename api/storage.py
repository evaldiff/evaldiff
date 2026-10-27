"""Object-storage abstraction: real S3/SeaweedFS when configured, else in-DB bytes.

v0 keeps things simple: the S3 client is written against boto3's minimal
surface (put/get/presign) so any S3-compatible endpoint (SeaweedFS, MinIO,
R2) works. When S3 is unset, datasets are stored inline in Postgres.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from functools import wraps


def observed_operation(method):
    @wraps(method)
    def observed(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            if self.error_callback is not None:
                self.error_callback()
            raise

    return observed


class Storage:
    """Interface used by the app. Two implementations, selected in config."""

    error_callback = None

    def put_json(self, key: str, obj: object) -> str: ...
    def get_json(self, key: str) -> object: ...
    def presigned_put_url(self, key: str, expires_in: int = 900) -> str: ...


class InMemoryStorage(Storage):
    """Dev/test storage — bytes live in process memory (or the DB via dataset row)."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    @observed_operation
    def put_json(self, key: str, obj: object) -> str:
        self._data[key] = json.dumps(obj, ensure_ascii=False).encode()
        return key

    @observed_operation
    def get_json(self, key: str) -> object:
        return json.loads(self._data[key].decode())

    @observed_operation
    def presigned_put_url(self, key: str, expires_in: int = 900) -> str:
        raise NotImplementedError("no presigned URLs without S3")


class S3Storage(Storage):
    """Real S3-compatible storage (SeaweedFS filer/S3, MinIO, R2...)."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "eu-central-1",
        timeout: float = 15.0,
    ) -> None:
        import boto3
        from botocore.config import Config

        # Transport-level timeouts for the S3 client (NEEDS-DOING C):
        # botocore defaults to 60 s for BOTH connect and read, and a
        # read timeout measures the time BETWEEN socket reads — a peer
        # that keeps sending data slowly can still outlive it, so the
        # caller's wait is bounded in api.runner, though the synchronous
        # call can continue after that wait expires. A timed-out call raises
        # (ClientError/ReadTimeoutError) and is treated as a failed
        # storage operation — the bounded pool + process-wide admission
        # gate (api.runner) cap the damage a stall can do.
        kwargs = {"signature_version": "s3v4"}
        if timeout and timeout > 0:
            kwargs.update(connect_timeout=timeout, read_timeout=timeout)
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(**kwargs),
        )
        self.bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:

        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001  (boto3 raises a dozen client-side types)
            kwargs = {"Bucket": self.bucket}
            # AWS-only needs LocationConstraint; most S3-compatible endpoints don't.
            try:
                self._s3.create_bucket(**kwargs)
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger("evaldiff.storage").debug("retrying bucket creation with region")
                self._s3.create_bucket(
                    **kwargs,
                    CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
                )

    @observed_operation
    def put_json(self, key: str, obj: object) -> str:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        return key

    @observed_operation
    def get_json(self, key: str) -> object:
        resp = self._s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(resp["Body"].read().decode())

    @observed_operation
    def presigned_put_url(self, key: str, expires_in: int = 900) -> str:
        return self._s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": "application/json",
            },
            ExpiresIn=expires_in,
        )


class LocalStorage(Storage):
    """Filesystem-backed storage for deployments without S3 (Part E).

    Evidence (case outputs, large datasets) must survive process restarts
    even when no S3 endpoint is configured; InMemoryStorage loses it on
    every restart. Files are written atomically (temp file + rename) so a
    crashed write never leaves a truncated object, and every read is
    bounded by a byte cap so a corrupt/huge object cannot blow up a
    report request.
    """

    DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB per object

    def __init__(self, root_dir: str, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = pathlib.Path(root_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def _path(self, key: str) -> pathlib.Path:
        if not key or key.startswith("/") or "\x00" in key:
            raise ValueError("invalid storage key")
        p = (self.root / key).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("storage key escapes the storage root")
        return p

    @observed_operation
    def put_json(self, key: str, obj: object) -> str:
        body = json.dumps(obj, ensure_ascii=False).encode()
        if len(body) > self.max_bytes:
            raise ValueError(f"object exceeds the {self.max_bytes}-byte storage limit")
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp.write_bytes(body)
            os.replace(tmp, p)  # atomic on POSIX: readers never see a partial file
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return key

    @observed_operation
    def get_json(self, key: str) -> object:
        p = self._path(key)
        if not p.is_file():
            raise KeyError(key)
        if p.stat().st_size > self.max_bytes:
            raise ValueError(f"object exceeds the {self.max_bytes}-byte storage limit")
        return json.loads(p.read_bytes().decode())

    @observed_operation
    def presigned_put_url(self, key: str, expires_in: int = 900) -> str:
        raise NotImplementedError("no presigned URLs with local storage")


def build_storage(settings) -> Storage:
    if settings.s3_endpoint:
        return S3Storage(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key or "",
            secret_key=settings.s3_secret_key or "",
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            timeout=float(getattr(settings, "storage_timeout", 0.0)),
        )
    if getattr(settings, "storage_dir", None):
        return LocalStorage(settings.storage_dir)
    return InMemoryStorage()
