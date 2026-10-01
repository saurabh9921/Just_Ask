"""Phase 4 end-to-end over the API: question -> plan -> validated SQL -> read-only
execution against the demo ERP (SQLite) -> formatted result, with history, audit,
idempotency, cancellation, streaming and the agent's limits."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import func, select

from app.agents.policies import AgentPolicy
from app.agents.state import AgentStep
from app.db.models import AuditEvent, QueryRecord
from app.llm.base import ChatMessage
from app.llm.mock_client import MockLLMProvider
from app.llm.mock_planner import plan_query
from app.query.plans import QueryPlan
from tests.conftest import AppFactory, bearer, create_account, login, resources_of
from tests.fixtures.demo_erp import seed_demo
from tests.unit.test_semantic_api import setup_indexed

ANCHOR = date(2026, 9, 15)
REGION_Q = "Show total sales by region for the last six months"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.analytics_service.utcnow", lambda: datetime(2026, 9, 15, 12, tzinfo=UTC)
    )


@pytest.fixture
async def demo_db(sqlite_data_dir: Path) -> str:
    url = f"sqlite+aiosqlite:///{(sqlite_data_dir / 'demo.db').as_posix()}"
    await seed_demo(url, anchor_date=ANCHOR, tenant_code="acme", scale=1)
    return "demo.db"


async def ask(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    cid: str,
    question: str,
    path: str = "query",
    **extra_headers: str,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/analytics/{path}",
        json={"question": question, "connection_id": cid},
        headers={**headers, **extra_headers},
    )


def script_plans(app: FastAPI, handler: Callable[[list[ChatMessage]], Any]) -> None:
    llm = resources_of(app).llm
    assert isinstance(llm, MockLLMProvider)
    llm.handlers[QueryPlan] = handler


async def records(app: FastAPI) -> list[QueryRecord]:
    async with resources_of(app).database.session_factory() as session:
        return list(await session.scalars(select(QueryRecord).order_by(QueryRecord.created_at)))


async def audit_events(app: FastAPI, event_type: str) -> list[AuditEvent]:
    async with resources_of(app).database.session_factory() as session:
        return list(
            await session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))
        )


# ------------------------------------------------------------------ happy paths
async def test_query_returns_a_governed_answer(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    r = await ask(client, viewer, cid, REGION_Q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["X-Query-Id"] == body["query_id"]
    assert body["status"] == "succeeded"
    assert body["connection"]["name"] == "Demo" and body["connection"]["connector_type"] == "sqlite"
    assert body["semantic_version"] == 1 and body["data_freshness"]
    assert body["source_entities"] == ["SalesOrder"]
    window = body["applied_filters"]["date_window"]
    assert window == {
        "field": "order_date",
        "start": "2026-03-16",
        "end_exclusive": "2026-09-16",
        "label": "2026-03-16 to 2026-09-15",
    }
    assert body["security_scope"] == {
        "tenant": "acme",
        "row_filter_fields": ["tenant_code"],
        "read_only": True,
    }
    assert body["validation"]["plan_valid"] and body["validation"]["sql_validated"]
    assert "tenant_code = ?" in body["sql"] and "acme" not in body["sql"]
    assert body["description"].startswith("total sales (sum of net_amount) by region")
    result = body["result"]
    assert body["rows_returned"] == result["row_count"] == 4
    assert [c["name"] for c in result["columns"]] == ["region", "total_sales"]
    assert result["chart"]["type"] == "bar" and result["kpis"][0]["scope"] == "total"
    assert body["explanation"]["source"] == "llm" and body["explanation"]["summary"]
    assert body["timings"]["total_ms"] > 0 and body["timings"]["execution_ms"] is not None
    steps = [s["step"] for s in body["steps"]]
    analysis_only = {AgentStep.COMPLETE, AgentStep.RESOLVE_ENTITIES, AgentStep.RUN_ANALYSIS}
    assert steps == [s.value for s in AgentStep if s not in analysis_only], (
        steps
    )  # every state of a planned answer, in order, no correction loop
    analysis = body["analysis"]  # validated: the breakdown is meaningful, the chart truthful
    assert analysis["validation"]["data_validated"] is True
    assert analysis["validation"]["chart_validated"] is True
    assert analysis["chart"] == {
        **analysis["chart"],
        "enabled": True,
        "type": "bar",
        "dimension": "Region",
    }
    [region] = analysis["dimensions"]
    assert region["usable"] and region["null_count"] == 0 and region["distinct_values"] == 4


async def test_comparison_and_trend_questions(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    cmp = (await ask(client, viewer, cid, "Compare this month's sales with last month")).json()
    assert cmp["status"] == "succeeded"
    assert cmp["applied_filters"]["previous_window"]["start"] == "2026-08-01"
    kpi = cmp["result"]["kpis"][0]
    assert kpi["previous"] is not None and kpi["change_pct"] is not None
    assert "previous period" in cmp["explanation"]["summary"]

    trend = (await ask(client, viewer, cid, "Monthly sales trend for the last 12 months")).json()
    assert trend["result"]["chart"]["type"] == "line"
    months = [row[0] for row in trend["result"]["rows"]]
    assert months == sorted(months) and len(months) == 13

    quarterly = (await ask(client, viewer, cid, "sales by quarter this year")).json()
    assert [row[0] for row in quarterly["result"]["rows"]] == [
        "2026-01-01",
        "2026-04-01",
        "2026-07-01",
    ]


async def test_history_record_and_audit_event_per_query(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    body = (await ask(client, viewer, cid, REGION_Q)).json()
    [record] = await records(app)
    assert str(record.id) == body["query_id"]
    assert record.status == "succeeded" and record.intent == "aggregation"
    assert record.normalized_plan_hash and record.compiled_sql_hash
    assert record.row_count == 4 and record.total_ms is not None and record.completed_at
    assert record.query_plan and record.query_plan["entity"] == "SalesOrder"
    [event] = await audit_events(app, "analytics.query")
    assert event.outcome == "success" and event.resource_id == body["query_id"]
    assert event.details["plan_hash"] == record.normalized_plan_hash
    assert event.details["sql_hash"] == record.compiled_sql_hash
    # No result values or SQL text in history/audit.
    stored = json.dumps(event.details) + json.dumps(record.query_plan)
    for value in ("South", "North", "SELECT", "acme"):
        assert value not in stored


async def test_get_query_returns_record_and_cached_response(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    got = (await client.get(f"/api/v1/analytics/query/{qid}", headers=viewer)).json()
    assert got["status"] == "succeeded" and got["row_count"] == 4
    assert got["response"]["result"]["row_count"] == 4 and not got["response_expired"]

    await resources_of(app).redis.flushall()
    expired = (await client.get(f"/api/v1/analytics/query/{qid}", headers=viewer)).json()
    assert expired["response"] is None and expired["response_expired"] is True


async def test_explain_plans_without_executing(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    body = (await ask(client, viewer, cid, REGION_Q, path="explain")).json()
    assert body["status"] == "planned"
    assert body["sql"] and body["validation"]["sql_validated"] and body["cost"]["source"]
    assert body["result"] is None and body["rows_returned"] is None
    steps = {s["step"] for s in body["steps"]}
    assert "EXECUTE_READONLY_QUERY" not in steps and "ESTIMATE_COST" in steps
    [record] = await records(app)
    assert record.status == "planned"
    assert len(await audit_events(app, "analytics.explain")) == 1


async def test_follow_up_refines_the_previous_plan(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    parent = (await ask(client, viewer, cid, REGION_Q)).json()
    r = await client.post(
        "/api/v1/analytics/follow-up",
        json={"question": "now break it down by branch", "parent_query_id": parent["query_id"]},
        headers=viewer,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["entity"] == "SalesOrder"
    assert body["plan"]["dimensions"] == ["branch"]
    assert body["applied_filters"]["date_window"] == parent["applied_filters"]["date_window"]
    assert body["connection"]["id"] == cid


async def test_follow_up_of_unknown_or_foreign_query_is_404(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    parent = (await ask(client, viewer, cid, REGION_Q)).json()
    r = await client.post(
        "/api/v1/analytics/follow-up",
        json={"question": "by branch", "parent_query_id": parent["query_id"]},
        headers=admin,  # another user of the same tenant
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ agent decisions
async def test_clarification_is_returned_not_executed(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    script_plans(
        app,
        lambda _: {"clarification_required": True, "clarification_question": "Which period?"},
    )
    body = (await ask(client, viewer, cid, "sales")).json()
    assert body["status"] == "clarification"
    assert body["clarification_question"] == "Which period?"
    assert body["sql"] is None and body["result"] is None
    [record] = await records(app)
    assert record.status == "clarification"


async def test_low_confidence_plan_asks_for_clarification(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    script_plans(
        app, lambda _: {"entity": "SalesOrder", "measures": ["total_sales"], "confidence": 0.1}
    )
    body = (await ask(client, viewer, cid, "how did we do")).json()
    assert body["status"] == "clarification" and body["validation"]["confidence"] == 0.1


async def test_invalid_plan_is_corrected_once(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    calls: list[int] = []

    def handler(messages: list[ChatMessage]) -> dict[str, Any]:
        calls.append(len(messages))
        if len(calls) == 1:
            return {
                "entity": "SalesOrder",
                "measures": ["total_sales", "profit_xyz"],
                "dimensions": ["region"],
                "date_range": {"field": "order_date", "relative": "last_month"},
            }
        return plan_query(messages)  # default correction: drop the rejected names

    script_plans(app, handler)
    body = (await ask(client, viewer, cid, "sales and profit by region last month")).json()
    assert body["status"] == "succeeded", body
    assert body["validation"]["corrections"] == 1
    assert body["plan"]["measures"] == ["total_sales"]
    assert len(calls) == 2 and calls[1] > calls[0]  # correction carries the rejected plan
    steps = [s["step"] for s in body["steps"]]
    assert steps.count("BUILD_QUERY_PLAN") == 2 and steps.count("VALIDATE_QUERY_PLAN") == 2


async def test_plan_still_invalid_after_correction_is_422(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    script_plans(app, lambda _: {"entity": "SalesOrder", "measures": ["zorbl_xyz"]})
    r = await ask(client, viewer, cid, "zorbl please")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "PLAN_INVALID" and err["details"]["query_id"]
    assert err["details"]["issues"][0]["code"] == "UNKNOWN_FIELD"
    [record] = await records(app)
    assert record.status == "failed" and record.error_code == "PLAN_INVALID"
    [event] = await audit_events(app, "analytics.query")
    assert event.outcome == "error"


async def test_step_timeout_stops_the_agent(
    app_factory: AppFactory, prepared_db: Any, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_factory(
        settings_override=prepared_db.model_copy(update={"agent_step_timeout_seconds": 0.3})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        _, viewer, cid = await setup_indexed(app, client, demo_db)
        llm = resources_of(app).llm
        original = llm.generate_structured

        async def slow(messages: list[ChatMessage], model: type[BaseModel], **kw: Any) -> Any:
            if model is QueryPlan:
                await asyncio.sleep(5)
            return await original(messages, model, **kw)

        monkeypatch.setattr(llm, "generate_structured", slow)
        r = await ask(client, viewer, cid, REGION_Q)
        assert r.status_code == 504, r.text
        err = r.json()["error"]
        assert err["code"] == "AGENT_LIMIT_EXCEEDED"
        assert err["details"] == {
            "reason": "STEP_TIMEOUT",
            "step": "BUILD_QUERY_PLAN",
            "query_id": err["details"]["query_id"],
        }
        assert len(await audit_events(app, "analytics.query")) == 1


async def test_max_steps_limit(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    real = AgentPolicy.from_settings
    monkeypatch.setattr(
        AgentPolicy, "from_settings", lambda s: dataclasses.replace(real(s), max_steps=4)
    )
    r = await ask(client, viewer, cid, REGION_Q)
    assert r.status_code == 504
    assert r.json()["error"]["details"]["reason"] == "MAX_STEPS"


# ------------------------------------------------------------------ cancel
async def _slow_planner(app: FastAPI, monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    llm = resources_of(app).llm
    original = llm.generate_structured

    async def slow(messages: list[ChatMessage], model: type[BaseModel], **kw: Any) -> Any:
        if model is QueryPlan:
            await asyncio.sleep(seconds)
        return await original(messages, model, **kw)

    monkeypatch.setattr(llm, "generate_structured", slow)


async def _wait_running(app: FastAPI) -> str:
    for _ in range(750):
        rows = await records(app)
        if (
            rows
            and rows[-1].status == "running"
            and resources_of(app).analytics_runs.get(rows[-1].id)
        ):
            return str(rows[-1].id)
        await asyncio.sleep(0.02)
    raise AssertionError("query never started")


async def test_cancel_running_query(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await _slow_planner(app, monkeypatch, 10)
    running = asyncio.create_task(ask(client, viewer, cid, REGION_Q))
    qid = await _wait_running(app)
    c = await client.post(f"/api/v1/analytics/query/{qid}/cancel", headers=viewer)
    assert c.status_code == 200 and c.json()["cancel_requested"] is True
    r = await asyncio.wait_for(running, 15)
    assert r.status_code == 409 and r.json()["error"]["code"] == "QUERY_CANCELLED"
    [record] = await records(app)
    assert record.status == "cancelled"
    assert len(await audit_events(app, "analytics.cancel")) == 1
    assert len(await audit_events(app, "analytics.query")) == 1  # still audited

    again = await client.post(f"/api/v1/analytics/query/{qid}/cancel", headers=viewer)
    assert again.json() == {"query_id": qid, "status": "cancelled", "cancel_requested": False}


async def test_cancel_flag_from_another_process(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await _slow_planner(app, monkeypatch, 10)
    monkeypatch.setattr(resources_of(app).settings, "query_cancel_poll_seconds", 0.05)
    running = asyncio.create_task(ask(client, viewer, cid, REGION_Q))
    qid = await _wait_running(app)
    await resources_of(app).redis.set(f"analytics:cancel:{qid}", "1")  # set by another API pod
    r = await asyncio.wait_for(running, 15)
    assert r.status_code == 409


async def test_cancel_unknown_query_is_404(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, _ = await setup_indexed(app, client, demo_db)
    r = await client.post(
        "/api/v1/analytics/query/00000000-0000-0000-0000-000000000001/cancel", headers=viewer
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ idempotency
async def test_idempotency_key_replays_the_first_outcome(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    first = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "k-1"})
    second = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "k-1"})
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["query_id"] == second.json()["query_id"]
    assert second.json()["result"] == first.json()["result"]
    assert len(await records(app)) == 1
    assert len(await audit_events(app, "analytics.query")) == 1

    conflict = await ask(client, viewer, cid, "other question", **{"Idempotency-Key": "k-1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    bad = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "x" * 129})
    assert bad.status_code == 400


async def test_idempotency_keys_are_per_user(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    a = await ask(client, viewer, cid, REGION_Q, **{"Idempotency-Key": "same"})
    b = await ask(client, admin, cid, REGION_Q, **{"Idempotency-Key": "same"})
    assert a.json()["query_id"] != b.json()["query_id"]
    assert "Idempotent-Replayed" not in b.headers


# ------------------------------------------------------------------ streaming
def parse_sse(text: str) -> list[tuple[str, Any]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


async def test_stream_emits_progress_then_result(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    r = await client.post(
        "/api/v1/analytics/query/stream",
        json={"question": REGION_Q, "connection_id": cid},
        headers=viewer,
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[0][0] == "accepted"
    progress = [data for name, data in events if name == "progress"]
    assert {p["step"] for p in progress} >= {"BUILD_QUERY_PLAN", "EXECUTE_READONLY_QUERY"}
    assert all(p["message"] and p["query_id"] == events[0][1]["query_id"] for p in progress)
    name, final = events[-1]
    assert name == "result" and final["status"] == "succeeded"
    assert final["result"]["row_count"] == 4
    [record] = await records(app)
    assert record.status == "succeeded"


async def test_stream_follow_up_and_error_event(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    parent = (await ask(client, viewer, cid, REGION_Q)).json()
    r = await client.post(
        "/api/v1/analytics/query/stream",
        json={"question": "by branch instead", "parent_query_id": parent["query_id"]},
        headers=viewer,
    )
    final = parse_sse(r.text)[-1]
    assert final[0] == "result" and final[1]["plan"]["dimensions"] == ["branch"]

    script_plans(app, lambda _: {"entity": "SalesOrder", "measures": ["nope"]})
    r = await client.post(
        "/api/v1/analytics/query/stream",
        json={"question": "nope", "connection_id": cid},
        headers=viewer,
    )
    name, data = parse_sse(r.text)[-1]
    assert name == "error" and data["error"]["code"] == "PLAN_INVALID"


async def test_stream_requires_a_source(app: FastAPI, client: httpx.AsyncClient) -> None:
    viewer = bearer(
        await login(
            client,
            await create_account(app, tenant_slug="acme", email="s@acme.test", roles=[]),
        )
    )
    r = await client.post("/api/v1/analytics/query/stream", json={"question": "hi"}, headers=viewer)
    assert r.status_code == 422


async def test_query_rate_limit(app_factory: AppFactory, prepared_db: Any, demo_db: str) -> None:
    app = app_factory(
        settings_override=prepared_db.model_copy(update={"query_rate_limit_per_user_per_minute": 2})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        _, viewer, cid = await setup_indexed(app, client, demo_db)
        codes = [(await ask(client, viewer, cid, REGION_Q)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        async with resources_of(app).database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(QueryRecord)) == 2


async def test_lost_vector_index_is_reported_clearly(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    resources = resources_of(app)
    await resources.redis.flushall()  # no cached retrievals
    for collection in await resources.qdrant.get_collections():
        for item in collection[1]:
            await resources.qdrant.delete_collection(item.name)
    r = await ask(client, viewer, cid, REGION_Q)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "SEMANTIC_INDEX_NOT_READY"


async def test_a_value_the_data_does_not_have_is_never_substituted(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    """The planner invents "profit_xyz"; the data has no profit: the user is told, with the
    measures that do exist - no other number is shown in its place."""
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    script_plans(app, lambda _: {"entity": "SalesOrder", "measures": ["profit_xyz"]})
    body = (await ask(client, viewer, cid, "profit please")).json()
    assert body["status"] == "clarification", body
    assert "no profit" in body["clarification_question"]
    assert "total sales" in body["clarification_question"]  # what *is* available
    assert body["result"] is None
