"""Dependency readiness checks.

Each check is a small object with a ``name`` and an async ``check()``. Checks run
concurrently with a per-check timeout; failures are reported as ``down`` without
the underlying error text (which may contain hostnames or credentials). The error
type is logged server-side for operators.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.core.metrics import DEPENDENCY_CHECK_DURATION, DEPENDENCY_UP

logger = get_logger(__name__)


class CheckStatus(StrEnum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    latency_ms: float
    required: bool


class HealthCheck(Protocol):
    name: str

    async def check(self) -> None:
        """Return normally when healthy; raise on failure."""
        ...


class PostgresHealthCheck:
    name = "postgres"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


class RedisHealthCheck:
    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> None:
        pong = await self._client.ping()
        if not pong:
            raise RuntimeError("redis ping failed")


class QdrantHealthCheck:
    name = "qdrant"

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    async def check(self) -> None:
        # Works for a remote server and for the in-process ``:memory:`` store.
        await self._client.get_collections()


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: tuple[CheckResult, ...]


class HealthService:
    def __init__(
        self,
        checks: list[HealthCheck],
        *,
        required: set[str],
        timeout_seconds: float,
    ) -> None:
        self._checks = checks
        self._required = required
        self._timeout = timeout_seconds

    async def _run_one(self, check: HealthCheck) -> CheckResult:
        started = time.perf_counter()
        status = CheckStatus.UP
        try:
            async with asyncio.timeout(self._timeout):
                await check.check()
        except Exception as exc:  # noqa: BLE001 - any failure means "down"
            status = CheckStatus.DOWN
            logger.warning(
                "readiness check failed",
                extra={"dependency": check.name, "error_type": type(exc).__name__},
            )
        elapsed = time.perf_counter() - started
        DEPENDENCY_CHECK_DURATION.labels(dependency=check.name, status=status.value).observe(
            elapsed
        )
        DEPENDENCY_UP.labels(dependency=check.name).set(1 if status is CheckStatus.UP else 0)
        return CheckResult(
            name=check.name,
            status=status,
            latency_ms=round(elapsed * 1000, 2),
            required=check.name in self._required,
        )

    async def readiness(self) -> ReadinessReport:
        results = await asyncio.gather(*(self._run_one(c) for c in self._checks))
        ready = all(r.status is CheckStatus.UP for r in results if r.required)
        # A required check that is not registered at all is a misconfiguration.
        registered = {c.name for c in self._checks}
        if not self._required.issubset(registered):
            ready = False
        return ReadinessReport(ready=ready, checks=tuple(results))
