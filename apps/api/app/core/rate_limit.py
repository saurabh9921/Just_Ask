"""Redis fixed-window rate limiting.

Keys are namespaced and hashed (``rl:<scope>:<sha256(identity)[:32]>:<window>``) so
emails or IPs never appear in Redis in clear text. Every key has a TTL.

Failure policy: if Redis is unavailable the limiter *fails open* and logs a warning.
Login remains protected by the database-backed account lockout, so an attacker
cannot gain unlimited guesses by taking Redis down; availability of sign-in is kept.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger
from app.core.metrics import RATE_LIMIT_REJECTIONS

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "rl") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, scope: str, identity: str, window_start: int) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return f"{self._prefix}:{scope}:{digest}:{window_start}"

    async def hit(
        self, scope: str, identity: str, *, limit: int, window_seconds: int = 60
    ) -> RateLimitDecision:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        key = self._key(scope, identity, window_start)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            logger.warning(
                "rate limiter unavailable; failing open",
                extra={"scope": scope, "error_type": type(exc).__name__},
            )
            return RateLimitDecision(allowed=True, remaining=limit, retry_after_seconds=0)

        count = int(count)
        retry_after = window_start + window_seconds - now
        return RateLimitDecision(
            allowed=count <= limit,
            remaining=max(0, limit - count),
            retry_after_seconds=max(1, retry_after),
        )

    async def enforce(
        self, scope: str, identity: str, *, limit: int, window_seconds: int = 60
    ) -> None:
        decision = await self.hit(scope, identity, limit=limit, window_seconds=window_seconds)
        if not decision.allowed:
            RATE_LIMIT_REJECTIONS.labels(scope=scope).inc()
            raise RateLimitedError(details={"retry_after_seconds": decision.retry_after_seconds})
