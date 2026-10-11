"""Model credential encryption. Production requires a stable Fernet key.

The primary key encrypts all new values. Previous keys are read-only fallbacks
for rotation. Values and keys must never appear in diagnostics.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .settings import Settings

_PREFIX = "enc:v1:"


class SecretError(RuntimeError):
    """A safe-to-display credential configuration or decryption failure."""


class KeyCipher:
    def __init__(self, settings: Settings):
        primary = settings.secret_key.get_secret_value().strip() if settings.secret_key else ""
        previous = (
            settings.previous_secret_keys.get_secret_value().strip()
            if settings.previous_secret_keys
            else ""
        )
        if not primary and (settings.production or previous):
            raise SecretError("EVALDIFF_SECRET_KEY is required for production or key rotation")
        self.primary = self._parse(primary, "EVALDIFF_SECRET_KEY") if primary else None
        old = []
        if previous:
            old = [
                self._parse(k.strip(), "EVALDIFF_PREVIOUS_SECRET_KEYS") for k in previous.split(",")
            ]
        self.reader = MultiFernet([self.primary, *old]) if self.primary else None

    @staticmethod
    def _parse(key: str, name: str) -> Fernet:
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise SecretError(f"{name} must contain valid Fernet keys") from None

    def encrypt(self, value: str) -> str:
        if not value or self.primary is None:
            return value
        return _PREFIX + self.primary.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str, *, allow_plaintext: bool = True) -> str:
        if not value:
            return value
        if not value.startswith(_PREFIX):
            if not allow_plaintext:
                raise SecretError(
                    "plaintext model credentials remain; run the credential migration"
                )
            return value
        if self.reader is None:
            raise SecretError("stored key is encrypted but EVALDIFF_SECRET_KEY is not set")
        try:
            return self.reader.decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise SecretError(
                "stored key could not be decrypted; check EVALDIFF_SECRET_KEY and previous keys"
            ) from None

    def uses_primary(self, value: str) -> bool:
        if self.primary is None or not value.startswith(_PREFIX):
            return False
        try:
            self.primary.decrypt(value[len(_PREFIX) :].encode("ascii"))
            return True
        except (InvalidToken, UnicodeError, ValueError):
            return False


def has_secret_key() -> bool:
    return KeyCipher(Settings()).primary is not None


def encrypt_api_key(value: str, *, settings: Settings | None = None) -> str:
    return KeyCipher(settings or Settings()).encrypt(value)


def decrypt_api_key(value: str, *, settings: Settings | None = None) -> str:
    settings = settings or Settings()
    # Preserve legacy local development reads, including when no key is configured.
    if not settings.production and not value.startswith(_PREFIX):
        return value
    return KeyCipher(settings).decrypt(value, allow_plaintext=not settings.production)


def generate_secret_key() -> str:
    return Fernet.generate_key().decode("ascii")
