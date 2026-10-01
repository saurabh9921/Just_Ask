"""Retry with backoff and a circuit breaker for calls to model/embedding servers.

Only *transient* failures are retried (timeouts, connection errors, 429/5xx).
Validation failures and 4xx responses are not: retrying a deterministic error
wastes capacity and can amplify an outage.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(DependencyUnavailableError):
    code = "CIRCUIT_OPEN"
    default_message = "The model service is temporarily unavailable. Please retry shortly."


@dataclass
class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures; after ``reset_seconds``
    one trial call is allowed (half-open). Success closes it; failure re-opens it."""

    name: str
    failure_threshold: int = 5
    reset_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _half_open_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self.clock() - self._opened_at >= self.reset_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def before_call(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            raise CircuitOpenError(details={"dependency": self.name})
        if state is CircuitState.HALF_OPEN:
            if self._half_open_in_flight:
                raise CircuitOpenError(details={"dependency": self.name})
            self._half_open_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        self._half_open_in_flight = False
        self._failures += 1
        if self._failures >= self.failure_threshold or self._opened_at is not None:
            if self._opened_at is None:
                logger.warning("circuit opened", extra={"dependency": self.name})
            self._opened_at = self.clock()


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError | TimeoutError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False


async def call_with_resilience[T](
    fn: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    max_retries: int,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``fn`` through the breaker, retrying transient failures with jittered backoff."""
    attempt = 0
    while True:
        breaker.before_call()
        try:
            result = await fn()
        except Exception as exc:
            if not is_transient(exc):
                # Deterministic failure: the dependency answered, so it is healthy.
                breaker.record_success()
                raise
            breaker.record_failure()
            if attempt >= max_retries:
                raise
            delay = min(max_delay, base_delay * (2**attempt)) * (0.5 + random.random() / 2)  # noqa: S311
            logger.info(
                "retrying transient failure",
                extra={
                    "dependency": breaker.name,
                    "attempt": attempt + 1,
                    "error_type": type(exc).__name__,
                },
            )
            attempt += 1
            await sleep(delay)
        else:
            breaker.record_success()
            return result
