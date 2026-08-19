"""API key authentication.

The scheme: a key is ``<key_id>.<secret>``, presented as a bearer token.  The
server stores only ``HMAC-SHA256(server_secret, secret)``, so a dump of the key
table does not let anyone sign requests.

Three details that are not optional:

* **Constant-time comparison.**  ``==`` on bytes short-circuits at the first
  differing byte, which leaks the length of the matching prefix.  Enough
  requests and that leak reconstructs the digest.
* **Hash, do not encrypt.**  There is never a reason for the server to recover
  a key's plaintext.
* **The verification cache is keyed by the digest, not the token.**  Keeping
  plaintext secrets in a long-lived dict undoes the point of hashing them.

HMAC-SHA256 is ~1us here, which is fine per request but not fine per request at
peak, so verified digests are cached briefly.  Note the trade: a revoked key
stays usable until its cache entry expires.  ``auth_cache_ttl`` is where you
choose your revocation latency.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from hashlib import sha256

from ..errors import Forbidden, Unauthorized
from ..telemetry.metrics import Registry
from ..util.clock import Clock
from ..util.idgen import IdGenerator
from ..util.lru import LRUCache


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller.  Handlers authorise against this, never
    against anything read out of the request body."""

    key_id: str
    account_id: str
    tier: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "admin" not in self.scopes:
            raise Forbidden(f"this API key lacks the {scope!r} scope", scope=scope)

    def owns(self, account_id: str) -> bool:
        return self.account_id == account_id or "admin" in self.scopes


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    account_id: str
    secret_digest: str
    tier: str
    scopes: frozenset[str]
    revoked: bool = False


class ApiKeyStore:
    """Issues and verifies API keys."""

    __slots__ = ("_secret", "_keys", "_cache", "_ids", "_clock", "_m_ok", "_m_fail")

    def __init__(
        self,
        server_secret: str,
        ids: IdGenerator,
        clock: Clock,
        registry: Registry,
        *,
        cache_size: int = 4096,
        cache_ttl: float = 30.0,
    ) -> None:
        self._secret = server_secret.encode("utf-8")
        self._keys: dict[str, ApiKeyRecord] = {}
        self._cache: LRUCache[str, Principal] = LRUCache(cache_size, ttl=cache_ttl)
        self._ids = ids
        self._clock = clock
        self._m_ok = registry.counter("auth_success_total", "requests authenticated")
        self._m_fail = registry.counter("auth_failure_total", "requests rejected by auth")

    def _digest(self, secret: str) -> str:
        return hmac.new(self._secret, secret.encode("utf-8"), sha256).hexdigest()

    def issue(
        self, account_id: str, tier: str = "standard", scopes: frozenset[str] | None = None
    ) -> tuple[str, ApiKeyRecord]:
        """Mint a key.  The plaintext is returned exactly once, here."""
        key_id = self._ids.next_token("key")
        secret = self._ids.next_token() + self._ids.next_token()
        record = ApiKeyRecord(
            key_id=key_id,
            account_id=account_id,
            secret_digest=self._digest(secret),
            tier=tier,
            scopes=scopes or frozenset({"trade", "read"}),
        )
        self._keys[key_id] = record
        return f"{key_id}.{secret}", record

    def revoke(self, key_id: str) -> bool:
        record = self._keys.get(key_id)
        if record is None:
            return False
        self._keys[key_id] = ApiKeyRecord(
            key_id=record.key_id,
            account_id=record.account_id,
            secret_digest=record.secret_digest,
            tier=record.tier,
            scopes=record.scopes,
            revoked=True,
        )
        self._cache.clear()  # revocation must be immediate, so drop the cache
        return True

    # --- HOT PATH ---------------------------------------------------------
    def authenticate(self, header_value: str | None) -> Principal:
        """Verify an ``Authorization`` header and return the caller."""
        if not header_value:
            self._m_fail.inc()
            raise Unauthorized("an Authorization header is required")

        scheme, _, token = header_value.partition(" ")
        if scheme.lower() != "bearer" or not token:
            self._m_fail.inc()
            raise Unauthorized("expected 'Authorization: Bearer <key>'")

        key_id, sep, secret = token.partition(".")
        if not sep:
            self._m_fail.inc()
            raise Unauthorized("malformed API key")

        digest = self._digest(secret)
        now = self._clock.monotonic()
        cached = self._cache.get(digest, now)
        if cached is not None and cached.key_id == key_id:
            self._m_ok.inc()
            return cached

        record = self._keys.get(key_id)
        # Verify the digest even when the key id is unknown, so that a bad id
        # and a bad secret take the same time to reject.
        expected = record.secret_digest if record is not None else digest[::-1]
        if not hmac.compare_digest(expected, digest) or record is None or record.revoked:
            self._m_fail.inc()
            raise Unauthorized("invalid API key")

        principal = Principal(
            key_id=record.key_id,
            account_id=record.account_id,
            tier=record.tier,
            scopes=record.scopes,
        )
        self._cache.put(digest, principal, now)
        self._m_ok.inc()
        return principal

    def cache_stats(self) -> dict[str, float]:
        return self._cache.stats()

    def count(self) -> int:
        return len(self._keys)
