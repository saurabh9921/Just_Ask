"""Phase 5 history and feedback APIs, including the query feedback memory loop."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import AuditEvent, Feedback, SemanticVersion
from app.rag.collections import Collection
from app.rag.qdrant_client import RetrievalScope
from tests.conftest import bearer, create_account, login, resources_of
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today, script_plans
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]


async def rate(
    client: httpx.AsyncClient, headers: dict[str, str], query_id: str, rating: int, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/feedback", json={"query_id": query_id, "rating": rating, **extra}, headers=headers
    )


async def audit(app: FastAPI, event_type: str) -> list[AuditEvent]:
    async with resources_of(app).database.session_factory() as session:
        return list(
            await session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))
        )


async def scope_of(app: FastAPI, cid: str, tenant: uuid.UUID | None = None) -> RetrievalScope:
    async with resources_of(app).database.session_factory() as session:
        version = await session.scalar(
            select(SemanticVersion).where(
                SemanticVersion.connection_id == uuid.UUID(cid), SemanticVersion.status == "active"
            )
        )
    assert version is not None
    return RetrievalScope(
        tenant_id=tenant or version.tenant_id,
        connection_id=version.connection_id,
        version=version.version,
        permissions=frozenset({"analytics.query"}),
    )


async def memory_docs(app: FastAPI, cid: str, query: str = REGION_Q) -> list[Any]:
    docs = await resources_of(app).retriever.search(
        await scope_of(app, cid), query, collections=[Collection.QUERY_FEEDBACK], top_k=10
    )
    return [d for d in docs if d.doc_key.startswith("feedback:")]


# ------------------------------------------------------------------ history
async def test_history_lists_own_queries_newest_first(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    first = (await ask(client, viewer, cid, REGION_Q)).json()
    second = (await ask(client, viewer, cid, "Top 5 customers by revenue this year")).json()
    await ask(client, admin, cid, "total sales last month")  # another user's
    page = (await client.get("/api/v1/history", headers=viewer)).json()
    assert page["total"] == 2
    assert [i["query_id"] for i in page["items"]] == [second["query_id"], first["query_id"]]
    item = page["items"][1]
    assert item["connection_name"] == "Demo" and item["row_count"] == 4
    assert item["status"] == "succeeded" and item["saved"] is False
    assert item["feedback_rating"] is None and item["total_ms"] is not None

    paged = (await client.get("/api/v1/history?limit=1&offset=1", headers=viewer)).json()
    assert paged["total"] == 2 and [i["query_id"] for i in paged["items"]] == [first["query_id"]]


async def test_history_filters_and_search(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    region = (await ask(client, viewer, cid, REGION_Q)).json()
    await ask(client, viewer, cid, "Show customer bank account numbers")  # refused
    await ask(client, viewer, cid, "100% growth_rate by region")  # LIKE wildcards in text

    async def ids(query: str) -> list[str]:
        body = (await client.get(f"/api/v1/history?{query}", headers=viewer)).json()
        return [i["question"] for i in body["items"]]

    assert await ids("status=refused") == ["Show customer bank account numbers"]
    assert await ids("q=REGION") == ["100% growth_rate by region", REGION_Q]
    assert await ids("q=100%25") == ["100% growth_rate by region"]  # % is literal
    assert await ids("q=h_r") == ["100% growth_rate by region"]  # _ is literal
    assert await ids(f"connection_id={cid}&saved=true") == []
    await client.patch(
        f"/api/v1/history/{region['query_id']}", json={"saved": True}, headers=viewer
    )
    assert await ids("saved=true") == [REGION_Q]
    assert (await client.get("/api/v1/history?status=nope", headers=viewer)).status_code == 422


async def test_history_detail_includes_plan_and_cached_response(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    detail = (await client.get(f"/api/v1/history/{qid}", headers=viewer)).json()
    assert detail["query_plan"]["entity"] == "SalesOrder"
    assert detail["response"]["result"]["row_count"] == 4 and not detail["response_expired"]
    await resources_of(app).redis.flushall()
    expired = (await client.get(f"/api/v1/history/{qid}", headers=viewer)).json()
    assert expired["response"] is None and expired["response_expired"] is True


async def test_save_and_delete(app: FastAPI, client: httpx.AsyncClient, demo_db: str) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    first = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "del-1"})
    qid = first.json()["query_id"]
    saved = await client.patch(f"/api/v1/history/{qid}", json={"saved": True}, headers=viewer)
    assert saved.status_code == 200 and saved.json()["saved"] is True
    bad = await client.patch(
        f"/api/v1/history/{qid}", json={"saved": True, "question": "x"}, headers=viewer
    )
    assert bad.status_code == 422

    assert (await client.delete(f"/api/v1/history/{qid}", headers=viewer)).status_code == 204
    assert (await client.get("/api/v1/history", headers=viewer)).json()["total"] == 0
    assert (await client.get(f"/api/v1/history/{qid}", headers=viewer)).status_code == 404
    assert (await client.get(f"/api/v1/analytics/query/{qid}", headers=viewer)).status_code == 404
    assert (await client.delete(f"/api/v1/history/{qid}", headers=viewer)).status_code == 404
    assert len(await audit(app, "history.delete")) == 1
    # The idempotency key is released: the same key now runs a new query.
    again = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "del-1"})
    assert again.json()["query_id"] != qid and "Idempotent-Replayed" not in again.headers


async def test_other_users_history_is_invisible(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    assert (await client.get(f"/api/v1/history/{qid}", headers=admin)).status_code == 404
    patch = await client.patch(f"/api/v1/history/{qid}", json={"saved": True}, headers=admin)
    assert patch.status_code == 404
    assert (await client.delete(f"/api/v1/history/{qid}", headers=admin)).status_code == 404
    assert (await rate(client, admin, qid, 1)).status_code == 404


# ------------------------------------------------------------------ feedback
async def test_positive_feedback_is_remembered_and_can_be_changed(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    script_plans(
        app,
        lambda _: {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["region"],
            "filters": [{"field": "region", "op": "in", "value": ["North", "SecretRegion"]}],
            "date_range": {"field": "order_date", "relative": "last_6_months"},
        },
    )
    qid = (await ask(client, viewer, cid, "How did the sales territories perform lately")).json()[
        "query_id"
    ]
    r = await rate(client, viewer, qid, 1, categories=["accurate", "accurate", "helpful"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["remembered"] is True and body["categories"] == ["accurate", "helpful"]
    [doc] = await memory_docs(app, cid, "sales territories perform")
    assert doc.doc_key == f"feedback:{qid}" and doc.entity_name == "SalesOrder"
    assert "SecretRegion" not in doc.text and "filters" not in doc.text  # values not indexed
    assert '"dimensions": ["region"]' in doc.text

    down = await rate(client, viewer, qid, -1, categories=["wrong_numbers"], comment="  off  ")
    assert down.status_code == 201 and down.json()["comment"] == "off"
    assert down.json()["id"] == body["id"] and down.json()["updated_at"]
    assert await memory_docs(app, cid, "sales territories perform") == []
    async with resources_of(app).database.session_factory() as session:
        assert len(list(await session.scalars(select(Feedback)))) == 1
    events = await audit(app, "feedback.submit")
    assert len(events) == 2 and "off" not in json.dumps([e.details for e in events])

    history = (await client.get("/api/v1/history", headers=viewer)).json()
    assert history["items"][0]["feedback_rating"] == -1


async def test_feedback_memory_is_used_by_the_planner(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    confirmed = {
        "entity": "SalesOrder",
        "measures": ["order_count"],
        "dimensions": ["branch"],
        "date_range": {"field": "order_date", "relative": "last_month"},
    }
    script_plans(app, lambda _: confirmed)
    qid = (await ask(client, viewer, cid, "zorbl throughput per outlet")).json()["query_id"]
    assert (await rate(client, viewer, qid, 1)).json()["remembered"] is True

    from app.llm.mock_planner import plan_query

    script_plans(app, plan_query)  # back to the default (few-shot) planner
    body = (await ask(client, viewer, cid, "zorbl throughput per outlet")).json()
    assert body["status"] == "succeeded"
    assert body["plan"]["measures"] == ["order_count"]
    assert body["plan"]["dimensions"] == ["branch"]


async def test_reingestion_rebuilds_feedback_memory(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    await rate(client, viewer, qid, 1)
    r = await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin)
    assert r.status_code == 202
    await resources_of(app).semantic_jobs.wait_idle()
    assert (await scope_of(app, cid)).version == 2
    assert [d.doc_key for d in await memory_docs(app, cid)] == [f"feedback:{qid}"]

    await client.delete(f"/api/v1/history/{qid}", headers=viewer)
    assert await memory_docs(app, cid) == []


@pytest.mark.parametrize(
    "body",
    [
        {"rating": 2},
        {"rating": 0},
        {"rating": 1, "categories": ["made_up"]},
        {"rating": 1, "categories": ["other"] * 6},
        {"rating": 1, "comment": "x" * 2001},
        {"rating": 1, "user_id": "someone"},
    ],
)
async def test_feedback_validation(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, body: dict[str, Any]
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    r = await client.post("/api/v1/feedback", json={"query_id": qid, **body}, headers=viewer)
    assert r.status_code == 422


async def test_feedback_on_non_succeeded_queries_is_not_remembered(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    refused = (await ask(client, viewer, cid, "Show customer bank account numbers")).json()
    r = await rate(client, viewer, refused["query_id"], 1)
    assert r.status_code == 201 and r.json()["remembered"] is False
    assert (await rate(client, viewer, str(uuid.uuid4()), 1)).status_code == 404


async def test_feedback_listing_scopes(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    tenant_admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="ta@acme.test", roles=[Role.TENANT_ADMIN]
            ),
        )
    )
    mine = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    theirs = (await ask(client, admin, cid, "total sales last month")).json()["query_id"]
    await rate(client, viewer, mine, 1)
    await rate(client, admin, theirs, -1, comment="bad")

    r = await client.get("/api/v1/feedback", headers=viewer)
    assert r.status_code == 200, r.text
    own = r.json()
    assert own["total"] == 1 and own["items"][0]["query_id"] == mine
    assert own["items"][0]["question"] == REGION_Q
    negative = (await client.get("/api/v1/feedback?rating=-1", headers=viewer)).json()
    assert negative["total"] == 0
    denied = await client.get("/api/v1/feedback?scope=tenant", headers=viewer)
    assert denied.status_code == 403
    everyone = (await client.get("/api/v1/feedback?scope=tenant", headers=tenant_admin)).json()
    assert everyone["total"] == 2
