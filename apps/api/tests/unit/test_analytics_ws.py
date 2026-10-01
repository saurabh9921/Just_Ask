"""Phase 5 WebSocket progress channel: auth handshake, multiplexed questions with
progress events, cancel, limits, re-authentication and disconnect handling."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db.models import AuditEvent, QueryRecord
from tests.conftest import resources_of
from tests.unit.test_analytics_api import REGION_Q, _slow_planner, demo_db, fixed_today
from tests.unit.test_semantic_api import setup_indexed
from tests.ws_client import WsClient

__all__ = ["demo_db", "fixed_today"]  # fixtures re-used from the API tests


def token_of(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def query(ref: str, cid: str, question: str = REGION_Q, **extra: Any) -> dict[str, Any]:
    return {"type": "query", "client_ref": ref, "question": question, "connection_id": cid, **extra}


async def records(app: FastAPI) -> list[QueryRecord]:
    async with resources_of(app).database.session_factory() as session:
        return list(await session.scalars(select(QueryRecord).order_by(QueryRecord.created_at)))


async def test_query_streams_progress_then_result(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        assert ws.accepted
        ready = await ws.authenticate(token_of(viewer))
        assert ready["type"] == "ready" and ready["tenant"] == "acme"
        await ws.send(query("r1", cid))
        result, frames = await ws.recv_until("result", client_ref="r1")
    accepted = frames[0]
    assert accepted["type"] == "accepted" and accepted["client_ref"] == "r1"
    progress = [f for f in frames if f["type"] == "progress"]
    steps = [p["event"]["step"] for p in progress if p["event"]["status"] == "completed"]
    assert steps[:3] == ["RECEIVE_QUESTION", "CLASSIFY_INTENT", "RESOLVE_DATA_SOURCE"]
    assert "EXECUTE_READONLY_QUERY" in steps
    assert all(p["query_id"] == accepted["query_id"] for p in progress)
    response = result["response"]
    assert response["status"] == "succeeded" and response["rows_returned"] == 4
    assert response["query_id"] == accepted["query_id"]


async def test_plan_mode_and_follow_up_over_the_socket(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send(query("p", cid, mode="plan"))
        planned, _ = await ws.recv_until("result", client_ref="p")
        assert planned["response"]["status"] == "planned"
        assert planned["response"]["result"] is None and planned["response"]["sql"]

        await ws.send(query("a", cid))
        parent, _ = await ws.recv_until("result", client_ref="a")
        await ws.send(
            {
                "type": "query",
                "client_ref": "f",
                "question": "now by branch",
                "parent_query_id": parent["query_id"],
            }
        )
        follow, _ = await ws.recv_until("result", client_ref="f")
    assert follow["response"]["plan"]["dimensions"] == ["branch"]


async def test_several_questions_are_multiplexed(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send(query("one", cid))
        await ws.send(query("two", cid, "Top 5 customers by revenue this year"))
        done: dict[str, dict[str, Any]] = {}
        while len(done) < 2:
            frame = await ws.recv(30)
            if frame.get("type") == "result":
                done[frame["client_ref"]] = frame
    assert done["one"]["response"]["rows_returned"] == 4
    assert done["two"]["response"]["rows_returned"] == 5
    assert done["one"]["query_id"] != done["two"]["query_id"]


async def test_query_errors_carry_the_client_ref(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send(query("x", "00000000-0000-0000-0000-000000000009"))
        err, _ = await ws.recv_until("error", client_ref="x")
        assert err["error"]["code"] == "NOT_FOUND" and err["error"]["status"] == 404

        await ws.send({"type": "query", "client_ref": "y", "question": "sales"})
        err, _ = await ws.recv_until("error", client_ref="y")
        assert err["error"]["code"] == "BAD_REQUEST"

        await ws.send(query("z", cid, "Show customer bank account numbers"))
        refused, _ = await ws.recv_until("result", client_ref="z")
        assert refused["response"]["status"] == "refused"


async def test_invalid_and_oversized_frames_do_not_close_the_socket(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send("not json {")
        assert (await ws.recv())["error"]["code"] == "INVALID_MESSAGE"
        await ws.send({"type": "query", "client_ref": "a b", "question": "q", "connection_id": cid})
        invalid = await ws.recv()
        assert invalid["error"]["code"] == "INVALID_MESSAGE"
        assert "a b" not in str(invalid)  # input is never echoed
        await ws.send({"type": "query", "client_ref": "r", "question": "x" * 20_000})
        assert (await ws.recv())["error"]["code"] == "MESSAGE_TOO_LARGE"
        await ws.send({"type": "ping"})
        assert await ws.recv() == {"type": "pong"}


async def test_too_many_concurrent_questions(
    app: FastAPI,
    client: httpx.AsyncClient,
    demo_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    monkeypatch.setattr(resources_of(app).settings, "ws_max_concurrent_queries", 1)
    await _slow_planner(app, monkeypatch, 1.0)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send(query("a", cid))
        await ws.send(query("b", cid))
        err, _ = await ws.recv_until("error", client_ref="b")
        assert err["error"]["code"] == "TOO_MANY_QUERIES"
        await ws.send(query("a", cid))  # still running under the same ref
        err, _ = await ws.recv_until("error", client_ref="a")
        assert err["error"]["code"] == "DUPLICATE_CLIENT_REF"
        await ws.recv_until("result", client_ref="a")


async def test_cancel_over_the_socket(
    app: FastAPI,
    client: httpx.AsyncClient,
    demo_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await _slow_planner(app, monkeypatch, 10)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send(query("c", cid))
        accepted, _ = await ws.recv_until("accepted", client_ref="c")
        for _ in range(250):  # wait until the run is registered for cancellation
            if resources_of(app).analytics_runs.get(uuid.UUID(accepted["query_id"])):
                break
            await asyncio.sleep(0.02)
        await ws.send({"type": "cancel", "query_id": accepted["query_id"]})
        cancelled, _ = await ws.recv_until("cancelled")
        assert cancelled["cancel_requested"] is True
        err, _ = await ws.recv_until("error", client_ref="c")
    assert err["error"]["code"] == "QUERY_CANCELLED" and err["error"]["status"] == 409
    assert err["response"]["status"] == "cancelled"
    [record] = await records(app)
    assert record.status == "cancelled"


async def test_disconnect_cancels_running_questions(
    app: FastAPI,
    client: httpx.AsyncClient,
    demo_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await _slow_planner(app, monkeypatch, 10)
    ws = WsClient(app)
    await ws.__aenter__()
    await ws.authenticate(token_of(viewer))
    await ws.send(query("d", cid))
    await ws.recv_until("accepted", client_ref="d")
    await asyncio.sleep(0.2)
    await ws.disconnect()
    [record] = await records(app)
    assert record.status == "cancelled"
    async with resources_of(app).database.session_factory() as session:
        events = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "analytics.query")
            )
        )
    assert len(events) == 1  # still audited


async def test_expired_token_is_refreshed_on_the_same_socket(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    tokens = resources_of(app).tokens
    claims = tokens.decode_access_token(token_of(viewer))
    stale, _ = tokens.issue_access_token(
        user_id=claims.user_id,
        tenant_id=claims.tenant_id,
        session_id=claims.session_id,
        now=datetime.now(tz=UTC) - timedelta(hours=1),
    )
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send({"type": "auth", "access_token": stale})
        assert (await ws.recv())["error"]["code"] == "TOKEN_EXPIRED"
        assert (await ws.authenticate(token_of(viewer)))["type"] == "ready"
        await ws.send(query("r", cid))
        result, _ = await ws.recv_until("result", client_ref="r")
    assert result["response"]["status"] == "succeeded"


async def test_socket_is_bound_to_one_principal(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, _ = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        other = await ws.authenticate(token_of(admin))
    assert other["type"] == "error" and other["error"]["code"] == "UNAUTHENTICATED"


async def test_handshake_requires_auth_first(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    async with WsClient(app) as ws:
        await ws.send(query("r", cid))
        assert await ws.recv() == {"__close__": 4401}
    async with WsClient(app) as ws:
        failed = await ws.authenticate("not-a-token")
        assert failed["error"]["code"] in {"INVALID_TOKEN", "UNAUTHENTICATED"}
        assert await ws.recv() == {"__close__": 4401}
    monkeypatch.setattr(resources_of(app).settings, "ws_auth_timeout_seconds", 0.1)
    async with WsClient(app) as ws:
        assert await ws.recv() == {"__close__": 4401}
    async with WsClient(app) as ws:
        assert (await ws.authenticate(token_of(viewer)))["type"] == "ready"
