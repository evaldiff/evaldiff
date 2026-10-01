"""Runtime settings for the evaldiff API."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVALDIFF_")

    database_url: str = "sqlite:///./evaldiff.db"

    # SeaweedFS / S3. When unset, datasets are stored inline in the DB.
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "evaldiff"
    s3_region: str = "eu-central-1"

    public_base_url: str = "http://localhost:8000"
    default_quota: int = 1000
    enable_worker: bool = True

    max_cases_per_dataset: int = 5000
    max_dataset_bytes: int = 10_000_000

    # Allow run endpoints that point at loopback / private / reserved address
    # ranges (self-hosted model servers on the same machine or LAN). Off by
    # default so public deployments cannot be used to probe internal services.
    # Deployment-administrator controlled only (env var EVALDIFF_ALLOW_LOCAL_ENDPOINTS);
    # API callers have no way to override this per-request.
    allow_local_endpoints: bool = False
