"""API keys: create, verify (sha256), and dependency for auth."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status

from .db import get_session
from .models import Account, ApiKey


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> tuple[str, str]:
    """Returns (display_key, hash). Display key is shown once at signup."""
    raw = secrets.token_urlsafe(32)
    key = f"eval_{raw}"
    return key, hash_key(key)


def ensure_key(session, account: Account, label: str = "default") -> str:
    """Return an existing key hash for the account (idempotent signup)."""
    existing = (
        session.query(ApiKey).filter(ApiKey.account_id == account.id, ApiKey.label == label).first()
    )
    return existing.key_hash if existing else ""


def create_key(session, account: Account, label: str = "default") -> tuple[str, str]:
    key, key_hash = generate_key()
    row = ApiKey(account_id=account.id, key_hash=key_hash, label=label)
    session.add(row)
    session.commit()
    return key, key_hash


def _resolve(session, auth: str) -> Account | None:
    if not auth:
        return None
    token = auth[7:] if auth.lower().startswith("bearer ") else auth
    digest = hash_key(token)
    key = session.query(ApiKey).filter(ApiKey.key_hash == digest).first()
    if not key:
        return None
    return session.get(Account, key.account_id)


def get_current_account(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session=Depends(get_session),
) -> Account:
    account = _resolve(session, authorization or "")
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
    return account
