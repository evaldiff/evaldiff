"""Fernet-at-rest for model API keys stored in ``runs.api_key_ref``.

Policy:
- ``EVALDIFF_SECRET_KEY`` set  → new keys are encrypted (``enc:v1:<token>``).
- not set                      → stored in plaintext, with a loud warning at
  startup. This keeps local development zero-config, while making the
  secure behaviour a one-line deployment change.

Legacy plaintext rows keep working: ``decrypt_api_key`` is a no-op for
values without the ``enc:v1:`` prefix, so upgrading mid-flight never
breaks already-stored runs.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def _get_fernet() -> Fernet | None:
    key = os.environ.get("EVALDIFF_SECRET_KEY", "").strip()
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def has_secret_key() -> bool:
    return _get_fernet() is not None


def encrypt_api_key(value: str) -> str:
    """Encrypt a model API key for storage. No-op when unset/empty."""
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    return _PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    """Decrypt a stored model API key. No-op for legacy plaintext rows."""
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value
    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "stored key is encrypted but EVALDIFF_SECRET_KEY is not set "
            "in this process — the key cannot be recovered"
        )
    try:
        return f.decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "stored key could not be decrypted — EVALDIFF_SECRET_KEY does not "
            "match the key it was encrypted with"
        ) from exc


def generate_secret_key() -> str:
    """CLI helper: print a new Fernet key for EVALDIFF_SECRET_KEY."""
    return Fernet.generate_key().decode("ascii")
