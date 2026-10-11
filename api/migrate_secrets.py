"""Offline, transactional credential migration: python -m api.migrate_secrets.

Stop all API/worker processes and back up the database and encryption keys first.
No credential values, encryption keys, or database URLs are printed.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import Text, inspect, select, update

from .db import make_engine
from .models import Run
from .secrets import _PREFIX, KeyCipher, SecretError
from .settings import Settings


@dataclass
class MigrationResult:
    checked: int = 0
    encrypted: int = 0
    rotated: int = 0


def _check_schema(conn) -> None:
    if not inspect(conn).has_table("runs"):
        raise SecretError("runs table is missing; initialize a new database with the application")
    if conn.dialect.name == "postgresql":
        column = next(c for c in inspect(conn).get_columns("runs") if c["name"] == "api_key_ref")
        if not isinstance(column["type"], Text):
            raise SecretError("credential column needs widening; run the credential migration")


def _credentials(conn):
    """Keyset batches avoid loading the complete credential table into memory."""
    last_id = -1
    while True:
        rows = conn.execute(
            select(Run.id, Run.api_key_ref).where(Run.id > last_id).order_by(Run.id).limit(500)
        ).all()
        if not rows:
            return
        yield from rows
        last_id = rows[-1].id


def verify_credentials(engine, cipher: KeyCipher, *, require_encrypted: bool) -> int:
    with engine.connect() as conn:
        if require_encrypted:
            _check_schema(conn)
        checked = 0
        for _, value in _credentials(conn):
            cipher.decrypt(value, allow_plaintext=not require_encrypted)
            checked += 1
        return checked


def migrate_credentials(engine, cipher: KeyCipher, *, rotate: bool = False) -> MigrationResult:
    if cipher.primary is None:
        raise SecretError("EVALDIFF_SECRET_KEY is required for credential migration")
    result = MigrationResult()
    # The exclusive lock serializes migrations and prevents intervening writes.
    # Operationally all API/workers must still be stopped: an old worker can
    # already hold a credential read before the lock was acquired.
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        elif conn.dialect.name == "postgresql":
            conn.exec_driver_sql("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE")
            # PostgreSQL DDL participates in this transaction; failures roll it back.
            column = next(
                c for c in inspect(conn).get_columns("runs") if c["name"] == "api_key_ref"
            )
            if not isinstance(column["type"], Text):
                conn.exec_driver_sql("ALTER TABLE runs ALTER COLUMN api_key_ref TYPE TEXT")
        else:
            raise SecretError("credential migration supports SQLite and PostgreSQL only")
        _check_schema(conn)
        for run_id, value in _credentials(conn):
            plain = cipher.decrypt(value)  # validate encrypted rows even when not rotating
            result.checked += 1
            if not value:
                continue
            if not value.startswith(_PREFIX):
                replacement = cipher.encrypt(plain)
                result.encrypted += 1
            elif rotate and not cipher.uses_primary(value):
                replacement = cipher.encrypt(plain)
                result.rotated += 1
            else:
                continue
            conn.execute(update(Run).where(Run.id == run_id).values(api_key_ref=replacement))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only schema and ciphertext check")
    mode.add_argument(
        "--rotate", action="store_true", help="also re-encrypt rows under the primary key"
    )
    mode.add_argument(
        "--validate-config", action="store_true", help="validate keys without opening the DB"
    )
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        cipher = KeyCipher(settings)
        if cipher.primary is None:
            raise SecretError("EVALDIFF_SECRET_KEY is required for credential operations")
        if args.validate_config:
            print("Encryption configuration valid.")
            return 0
        engine = make_engine(settings.database_url)
        if args.check:
            count = verify_credentials(engine, cipher, require_encrypted=True)
            print(f"Verified {count} credential rows.")
        else:
            result = migrate_credentials(engine, cipher, rotate=args.rotate)
            print(
                f"Checked {result.checked} rows; encrypted {result.encrypted}; rotated {result.rotated}."
            )
        return 0
    except SecretError as exc:
        print(f"Credential operation failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 -- keep database values out of CLI diagnostics
        # SQL errors can include values/URLs; never print the exception or traceback.
        print(
            "Credential operation failed; check database access and schema. No changes committed.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
