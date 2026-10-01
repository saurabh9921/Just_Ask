"""End-to-end Phase 3 path over the API: demo ERP → metadata sync → semantic
ingestion (in-memory Qdrant, hashing embeddings) → hybrid search → LLM entity
resolution (mock provider) with re-validation."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.permissions import Role
from app.core.security import utcnow
from app.db.models import SemanticVersion
from app.rag.collections import Collection
from app.rag.qdrant_client import ALL_PERMISSIONS, RetrievalScope
from app.workers.jobs import STALE_JOB_AFTER
from tests.conftest import bearer, create_account, login, resources_of
from tests.fixtures.demo_erp import seed_demo

ANCHOR = date(2026, 9, 15)


@pytest.fixture
async def demo_db(sqlite_data_dir: Path) -> str:
    path = sqlite_data_dir / "demo.db"
    await seed_demo(f"sqlite+aiosqlite:///{path.as_posix()}", anchor_date=ANCHOR, scale=1)
    return "demo.db"


async def setup_indexed(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, tenant: str = "acme"
) -> tuple[dict[str, str], dict[str, str], str]:
    admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug=tenant, email=f"da@{tenant}.test", roles=[Role.DATA_ADMIN]
            ),
        )
    )
    viewer = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug=tenant, email=f"v@{tenant}.test", roles=[Role.VIEWER]
            ),
        )
    )
    conn = (
        await client.post(
            "/api/v1/connections",
            json={"name": "Demo", "connector_type": "sqlite", "database_name": demo_db},
            headers=admin,
        )
    ).json()
    cid = conn["id"]
    assert (
        await client.post(f"/api/v1/connections/{cid}/metadata/sync", headers=admin)
    ).status_code == 202
    await resources_of(app).metadata_jobs.wait_idle()
    r = await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin)
    assert r.status_code == 202, r.text
    await resources_of(app).semantic_jobs.wait_idle()
    return admin, viewer, cid


async def search(
    client: httpx.AsyncClient, headers: dict[str, str], cid: str, query: str, **body: Any
) -> dict[str, Any]:
    r = await client.post(
        f"/api/v1/connections/{cid}/semantic/search", json={"query": query, **body}, headers=headers
    )
    assert r.status_code == 200, r.text
    result: dict[str, Any] = r.json()
    return result


async def test_ingestion_builds_an_active_version(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, _, cid = await setup_indexed(app, client, demo_db)
    status = (await client.get(f"/api/v1/connections/{cid}/semantic/status", headers=admin)).json()
    latest = status["latest"]
    assert latest["status"] == "active", latest
    assert status["active_version"] == 1
    assert latest["embedding_model"] == "hashing" and latest["embedding_dimensions"] == 256
    counts = latest["document_counts"]
    for collection in (
        "semantic_entities",
        "business_glossary",
        "query_examples",
        "database_metadata",
    ):
        assert counts.get(collection, 0) > 0, counts
    assert all(b["bound"] for b in latest["entity_bindings"].values())


async def test_hybrid_search_finds_synonyms_codes_and_examples(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    territory = await search(
        client, viewer, cid, "sales by territory", collections=["semantic_entities"]
    )
    assert any(h["doc_key"] == "dimension:SalesOrder.region" for h in territory["hits"])

    abbr = await search(client, viewer, cid, "AOV", collections=["business_glossary"], top_k=3)
    assert abbr["hits"][0]["doc_key"] in {"abbr:aov", "term:average order value"}
    assert abbr["hits"][0]["sparse_rank"] == 1  # exact-term match comes from the sparse leg

    examples = await search(
        client, viewer, cid, "products below minimum stock", collections=["query_examples"]
    )
    assert examples["hits"] and examples["hits"][0]["entity_name"] == "Inventory"


async def test_resolve_maps_question_to_governed_vocabulary(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    r = await client.post(
        f"/api/v1/connections/{cid}/semantic/resolve",
        json={"question": "Show total sales by region for the last six months"},
        headers=viewer,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"]["intent"] == "aggregation"
    assert body["resolution"]["entity"] == "SalesOrder"
    assert "total_sales" in body["resolution"]["measures"]
    assert "region" in body["resolution"]["dimensions"]
    assert body["rejected_names"] == []
    assert body["llm_provider"] == "mock" and body["context"]


async def test_model_output_outside_vocabulary_is_rejected(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    from app.agents.schemas import EntityResolution
    from app.llm.mock_client import MockLLMProvider

    _, viewer, cid = await setup_indexed(app, client, demo_db)
    resources_of(app).llm = MockLLMProvider(
        {
            EntityResolution: lambda _m: {
                "entity": "SalesOrder",
                "measures": ["total_sales", "salary_total"],
                "dimensions": ["region", "customer_bank_account"],
                "time_field": "deleted_at",
                "confidence": 0.99,
            }
        }
    )
    body = (
        await client.post(
            f"/api/v1/connections/{cid}/semantic/resolve",
            json={"question": "sales"},
            headers=viewer,
        )
    ).json()
    assert body["resolution"]["measures"] == ["total_sales"]
    assert body["resolution"]["dimensions"] == ["region"]
    assert body["resolution"]["time_field"] is None
    assert sorted(body["rejected_names"]) == ["customer_bank_account", "deleted_at", "salary_total"]
    assert body["resolution"]["confidence"] <= 0.5

    resources_of(app).llm = MockLLMProvider(
        {
            EntityResolution: lambda _m: {
                "entity": "Payroll",
                "measures": ["salary"],
                "confidence": 0.9,
            }
        }
    )
    body = (
        await client.post(
            f"/api/v1/connections/{cid}/semantic/resolve",
            json={"question": "salaries"},
            headers=viewer,
        )
    ).json()
    assert body["resolution"]["entity"] is None
    assert body["rejected_names"] == ["Payroll", "salary"]


async def test_reingestion_activates_new_version_and_removes_stale_vectors(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, _, cid = await setup_indexed(app, client, demo_db)
    await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin)
    await resources_of(app).semantic_jobs.wait_idle()
    status = (await client.get(f"/api/v1/connections/{cid}/semantic/status", headers=admin)).json()
    assert status["active_version"] == 2

    import uuid

    res = resources_of(app)
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    tenant = uuid.UUID(me["tenant"]["id"])
    for version, expect_empty in ((1, True), (2, False)):
        scope = RetrievalScope(tenant, uuid.UUID(cid), version, ALL_PERMISSIONS)
        count = await res.vector_store.count(Collection.SEMANTIC_ENTITIES, scope)
        assert (count == 0) is expect_empty, (version, count)


async def test_preconditions_and_permissions(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
            ),
        )
    )
    viewer = bearer(
        await login(
            client,
            await create_account(app, tenant_slug="acme", email="v@acme.test", roles=[Role.VIEWER]),
        )
    )
    cid = (
        await client.post(
            "/api/v1/connections",
            json={"name": "Demo", "connector_type": "sqlite", "database_name": demo_db},
            headers=admin,
        )
    ).json()["id"]
    no_sync = await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin)
    assert no_sync.json()["error"]["code"] == "METADATA_SYNC_REQUIRED"
    not_ready = await client.post(
        f"/api/v1/connections/{cid}/semantic/search", json={"query": "sales"}, headers=viewer
    )
    assert not_ready.json()["error"]["code"] == "SEMANTIC_INDEX_NOT_READY"
    denied = await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=viewer)
    assert denied.status_code == 403
    too_long = await client.post(
        f"/api/v1/connections/{cid}/semantic/search", json={"query": "x" * 1001}, headers=viewer
    )
    assert too_long.status_code == 422


async def test_retrieval_cache_is_used_on_repeat(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    first = await search(client, viewer, cid, "overdue invoices")
    keys = [k async for k in resources_of(app).redis.scan_iter("rag:*")]
    assert len(keys) == 1
    second = await search(client, viewer, cid, "  OVERDUE   invoices ")  # normalised → same key
    assert first == second
    assert len([k async for k in resources_of(app).redis.scan_iter("rag:*")]) == 1


async def test_abandoned_build_is_superseded(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, _, cid = await setup_indexed(app, client, demo_db)
    orphan_id = uuid.uuid4()
    async with resources_of(app).database.session_factory() as session:
        active = await session.get(
            SemanticVersion,
            uuid.UUID(
                (
                    await client.get(f"/api/v1/connections/{cid}/semantic/status", headers=admin)
                ).json()["latest"]["version_id"]
            ),
        )
        assert active is not None
        started = utcnow() - STALE_JOB_AFTER - timedelta(minutes=1)
        session.add(
            SemanticVersion(
                id=orphan_id,
                tenant_id=active.tenant_id,
                connection_id=active.connection_id,
                version=2,
                status="building",
                document_counts={},
                entity_bindings={},
                created_at=started,
                started_at=started,
            )
        )
        await session.commit()

    r = await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin)
    assert r.status_code == 202, r.text
    assert r.json()["version"] == 3
    await resources_of(app).semantic_jobs.wait_idle()
    status = (await client.get(f"/api/v1/connections/{cid}/semantic/status", headers=admin)).json()
    assert status["active_version"] == 3
    async with resources_of(app).database.session_factory() as session:
        orphan = await session.get(SemanticVersion, orphan_id)
        assert orphan is not None
        assert (orphan.status, orphan.error_code) == ("failed", "ABANDONED")
