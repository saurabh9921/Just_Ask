"""Phase 6 caching and tenant-level rate limiting."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import AuditEvent, QueryRecord
from app.query.result_cache import CatalogCache, CatalogEntry, result_cache_key, scope_hash
from tests.conftest import AppFactory, bearer, create_account, login, resources_of
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]


def key(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "tenant_id": uuid.UUID(int=1),
        "connection_id": uuid.UUID(int=2),
        "config_version": "cfg",
        "semantic_version": 3,
        "scope": scope_hash(frozenset({"analytics.query"}), [("t0", "tenant_code", "acme")]),
        "plan_hash": "p" * 64,
        "sql_hash": "s" * 64,
    }
    values.update(overrides)
    return result_cache_key(**values)


def test_cache_key_covers_tenant_connection_version_scope_and_plan() -> None:
    base = key()
    assert base.startswith(f"qr:{uuid.UUID(int=1)}:{uuid.UUID(int=2)}:")
    variants = [
        key(tenant_id=uuid.UUID(int=9)),
        key(connection_id=uuid.UUID(int=9)),
        key(config_version="other"),
        key(semantic_version=4),
        key(scope=scope_hash(frozenset({"analytics.query"}), [("t0", "tenant_code", "globex")])),
        key(scope=scope_hash(frozenset({"analytics.query", "admin.manage"}), [])),
        key(plan_hash="q" * 64),
        key(sql_hash="t" * 64),
    ]
    assert len({base, *variants}) == len(variants) + 1


def test_catalog_cache_is_a_bounded_lru() -> None:
    cache = CatalogCache(max_entries=2)
    tenant = uuid.uuid4()
    snaps = [uuid.uuid4() for _ in range(3)]
    for snap in snaps:
        cache.put_catalog(tenant, snap, CatalogEntry([], {}))
    assert cache.get_catalog(tenant, snaps[0]) is None  # evicted
    assert cache.get_catalog(tenant, snaps[2]) is not None
    assert cache.get_catalog(uuid.uuid4(), snaps[2]) is None  # keyed by tenant too
    assert len(cache) == 2
    disabled = CatalogCache(max_entries=0)
    disabled.put_catalog(tenant, snaps[0], CatalogEntry([], {}))
    assert disabled.get_catalog(tenant, snaps[0]) is None


async def test_repeated_question_is_served_from_the_result_cache(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    first = (await ask(client, viewer, cid, REGION_Q)).json()
    second = (await ask(client, viewer, cid, REGION_Q)).json()
    assert first["cache"] == {"hit": False, "age_seconds": None}
    assert second["cache"]["hit"] is True and second["cache"]["age_seconds"] is not None
    assert second["result"]["rows"] == first["result"]["rows"]
    assert second["timings"]["execution_ms"] == 0.0
    assert second["query_id"] != first["query_id"]  # still a separate, audited query
    async with resources_of(app).database.session_factory() as session:
        records = list(await session.scalars(select(QueryRecord).order_by(QueryRecord.created_at)))
        events = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "analytics.query")
            )
        )
    assert [r.cache_hit for r in records] == [False, True]
    assert sorted(e.details["cache_hit"] for e in events) == [False, True]


async def test_cache_is_shared_within_a_tenant_scope_only(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    await ask(client, viewer, cid, REGION_Q)
    # Same tenant, same permissions set? data_admin has more permissions -> other scope.
    other_scope = (await ask(client, admin, cid, REGION_Q)).json()
    assert other_scope["cache"]["hit"] is False
    colleague = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="v2@acme.test", roles=[Role.VIEWER]
            ),
        )
    )
    same_scope = (await ask(client, colleague, cid, REGION_Q)).json()
    assert same_scope["cache"]["hit"] is True


async def test_redis_failure_degrades_to_a_miss(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await ask(client, viewer, cid, REGION_Q)
    redis = resources_of(app).redis

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "get", broken)
    body = (await ask(client, viewer, cid, REGION_Q)).json()
    assert body["status"] == "succeeded" and body["cache"]["hit"] is False


async def test_result_cache_can_be_disabled(
    app_factory: AppFactory, prepared_db: Any, demo_db: str
) -> None:
    app = app_factory(
        settings_override=prepared_db.model_copy(update={"query_result_cache_enabled": False})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        _, viewer, cid = await setup_indexed(app, client, demo_db)
        await ask(client, viewer, cid, REGION_Q)
        assert (await ask(client, viewer, cid, REGION_Q)).json()["cache"]["hit"] is False


async def test_connection_change_invalidates_cached_results(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    await ask(client, viewer, cid, REGION_Q)
    r = await client.patch(f"/api/v1/connections/{cid}", json={"max_rows": 900}, headers=admin)
    assert r.status_code == 200, r.text
    assert (await ask(client, viewer, cid, REGION_Q)).json()["cache"]["hit"] is False


async def test_tenant_rate_limit_spans_users(
    app_factory: AppFactory, prepared_db: Any, demo_db: str
) -> None:
    app = app_factory(
        settings_override=prepared_db.model_copy(
            update={"query_rate_limit_per_tenant_per_minute": 2}
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        admin, viewer, cid = await setup_indexed(app, client, demo_db)
        assert (await ask(client, viewer, cid, REGION_Q)).status_code == 200
        assert (await ask(client, admin, cid, REGION_Q)).status_code == 200
        limited = await ask(client, viewer, cid, "total sales last month")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
