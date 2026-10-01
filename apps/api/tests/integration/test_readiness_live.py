"""Readiness checks against real Redis and Qdrant."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from app.rag.qdrant_client import build_qdrant_client
from app.services.health_service import (
    CheckStatus,
    HealthService,
    QdrantHealthCheck,
    RedisHealthCheck,
)
from tests.integration.conftest import require_env

pytestmark = pytest.mark.integration


async def test_redis_check_against_live_redis() -> None:
    client = Redis.from_url(require_env("TEST_REDIS_URL"))
    try:
        report = await HealthService(
            [RedisHealthCheck(client)], required={"redis"}, timeout_seconds=2
        ).readiness()
    finally:
        await client.aclose()
    assert report.ready
    assert report.checks[0].status is CheckStatus.UP


async def test_qdrant_check_against_live_qdrant() -> None:
    url = require_env("TEST_QDRANT_URL")
    client = build_qdrant_client(url, None)
    try:
        report = await HealthService(
            [QdrantHealthCheck(client)], required={"qdrant"}, timeout_seconds=2
        ).readiness()
    finally:
        await client.close()
    assert report.ready


async def test_unreachable_redis_reports_down() -> None:
    require_env("TEST_REDIS_URL")  # only meaningful alongside the live suite
    client = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    try:
        report = await HealthService(
            [RedisHealthCheck(client)], required={"redis"}, timeout_seconds=1
        ).readiness()
    finally:
        await client.aclose()
    assert not report.ready
