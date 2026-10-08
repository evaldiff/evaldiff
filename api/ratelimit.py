"""Per-account token-bucket rate limiting (auth endpoints) + per-IP bucket
for signup (unauthenticated).

Design notes:
- In-memory: correct for a single-process deployment (v0). With Postgres +
  multiple workers the buckets split per worker — a known v0 trade-off; the
  limits are an anti-abuse backstop, not an exact QoS contract.
- Buckets are created on first use and never evicted in v0 (account count
  is small; a cap + LRU eviction is the next step when it matters).
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, max ``capacity`` tokens."""

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = max(rate, 0.0)
        self.capacity = max(capacity, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> float:
        """Consume one token. Returns 0.0 on success, or the seconds until
        a token will be available (caller should 429 with that Retry-After)."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            deficit = 1.0 - self._tokens
            return deficit / self.rate if self.rate > 0 else float("inf")


class _BucketRegistry:
    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = TokenBucket(self._rate, self._capacity)
                self._buckets[key] = b
        return b.take()


class RateLimiter:
    """Two registries: per-account (authenticated) and per-IP (signup)."""

    def __init__(self, *, rpm: float, burst: int, signup_per_min: float, signup_burst: int) -> None:
        self.accounts = _BucketRegistry(rpm, burst)
        self.signup = _BucketRegistry(signup_per_min / 60.0, signup_burst)

    def take_account(self, account_id: int) -> float:
        return self.accounts.take(f"acct:{account_id}")

    def take_signup(self, client_ip: str) -> float:
        return self.signup.take(f"ip:{client_ip or 'unknown'}")
