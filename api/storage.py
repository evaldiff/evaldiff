"""Object-storage abstraction: real S3/SeaweedFS when configured, else in-DB bytes.

v0 keeps things simple: the S3 client is written against boto3's minimal
surface (put/get/presign) so any S3-compatible endpoint (SeaweedFS, MinIO,
R2) works. When S3 is unset, datasets are stored inline in Postgres.
"""

from __future__ import annotations

import json


class Storage:
    """Interface used by the app. Two implementations, selected in config."""

    def put_json(self, key: str, obj: object) -> str: ...
    def get_json(self, key: str) -> object: ...
    def presigned_put_url(self, key: str, expires_in: int = 900) -> str: ...


class InMemoryStorage(Storage):
    """Dev/test storage — bytes live in process memory (or the DB via dataset row)."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put_json(self, key: str, obj: object) -> str:
        self._data[key] = json.dumps(obj, ensure_ascii=False).encode()
        return key

    def get_json(self, key: str) -> object:
        return json.loads(self._data[key].decode())

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
    ) -> None:
        import boto3

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=boto3.Config(signature_version="s3v4"),
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
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger("evaldiff.storage").debug("no region needed: %s", exc)
                self._s3.create_bucket(
                    **kwargs,
                    CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
                )

    def put_json(self, key: str, obj: object) -> str:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        return key

    def get_json(self, key: str) -> object:
        resp = self._s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(resp["Body"].read().decode())

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


def build_storage(settings) -> Storage:
    if settings.s3_endpoint:
        return S3Storage(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key or "",
            secret_key=settings.s3_secret_key or "",
            bucket=settings.s3_bucket,
            region=settings.s3_region,
        )
    return InMemoryStorage()
