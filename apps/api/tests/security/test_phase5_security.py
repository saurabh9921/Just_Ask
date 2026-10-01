"""Phase 5 security tests: WebSocket channel (origin, in-band auth, session
revalidation, no input echo), history/feedback tenant and user isolation, feedback
memory scoping, and injection through history search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import QueryRecord
from app.rag.collections import Collection
from tests.conftest import bearer, create_account, login, resources_of
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today
from tests.unit.test_analytics_ws import query, token_of
from tests.unit.test_history_feedback_api import rate, scope_of
from tests.unit.test_semantic_api import setup_indexed
from tests.ws_client import WsClient

pytestmark = pytest.mark.security
__all__ = ["demo_db", "fixed_today"]


# ------------------------------------------------------------------ WebSocket
@pytest.mark.parametrize(
    "origin", ["https://evil.example", "chrome-extension://someotherextensionidxxxxxxxxxx", "null"]
)
async def test_websocket_rejects_foreign_origins(app: FastAPI, origin: str) -> None:
    async with WsClient(app, origin=origin) as ws:
        assert not ws.accepted
        assert ws.close_code == 1008


async def test_websocket_without_origin_still_needs_auth(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, _ = await setup_indexed(app, client, demo_db)
    async with WsClient(app, origin=None) as ws:
        assert ws.accepted
        assert (await ws.authenticate(token_of(viewer)))["type"] == "ready"


async def test_token_in_the_url_is_ignored(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    ws = WsClient(app)
    ws.scope["query_string"] = f"access_token={token_of(viewer)}".encode()
    async with ws:
        await ws.send(query("r", cid))
        assert await ws.recv() == {"__close__": 4401}
    async with resources_of(app).database.session_factory() as session:
        assert list(await session.scalars(select(QueryRecord))) == []


async def test_revoked_session_is_enforced_on_an_open_socket(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    await setup_indexed(app, client, demo_db)
    account = await create_account(
        app, tenant_slug="acme", email="ws@acme.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, account)
    headers = bearer(tokens)
    cid = (await client.get("/api/v1/connections", headers=headers)).json()["items"][0]["id"]
    async with WsClient(app) as ws:
        assert (await ws.authenticate(tokens["access_token"]))["type"] == "ready"
        logout = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        )
        assert logout.status_code in {200, 204}
        await ws.send(query("r", cid))
        err, _ = await ws.recv_until("error", client_ref="r")
    assert err["error"]["code"] == "SESSION_REVOKED"
    async with resources_of(app).database.session_factory() as session:
        assert list(await session.scalars(select(QueryRecord))) == []


async def test_error_frames_never_echo_input(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, _ = await setup_indexed(app, client, demo_db)
    payload = "<script>alert(1)</script>"
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(viewer))
        await ws.send({"type": "query", "client_ref": payload, "question": payload})
        frame = await ws.recv()
        await ws.send({"type": payload})
        frame2 = await ws.recv()
    for f in (frame, frame2):
        assert f["error"]["code"] == "INVALID_MESSAGE"
        assert "script" not in str(f)


async def test_cross_tenant_query_ids_cannot_be_cancelled_over_the_socket(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, acme, acme_cid = await setup_indexed(app, client, demo_db, tenant="acme")
    _, globex, _ = await setup_indexed(app, client, demo_db, tenant="globex")
    qid = (await ask(client, acme, acme_cid, REGION_Q)).json()["query_id"]
    async with WsClient(app) as ws:
        await ws.authenticate(token_of(globex))
        await ws.send({"type": "cancel", "query_id": qid})
        err = await ws.recv()
        await ws.send(query("x", acme_cid))
        err2, _ = await ws.recv_until("error", client_ref="x")
    assert err["error"]["code"] == "NOT_FOUND"
    assert err2["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ history / feedback
async def test_history_and_feedback_are_tenant_isolated(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, acme, acme_cid = await setup_indexed(app, client, demo_db, tenant="acme")
    _, globex, _ = await setup_indexed(app, client, demo_db, tenant="globex")
    qid = (await ask(client, acme, acme_cid, REGION_Q)).json()["query_id"]
    await rate(client, acme, qid, 1, comment="acme-only note")

    assert (await client.get("/api/v1/history", headers=globex)).json()["total"] == 0
    assert (await client.get(f"/api/v1/history/{qid}", headers=globex)).status_code == 404
    assert (await client.delete(f"/api/v1/history/{qid}", headers=globex)).status_code == 404
    assert (await rate(client, globex, qid, -1)).status_code == 404
    globex_admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="globex", email="ta@globex.test", roles=[Role.TENANT_ADMIN]
            ),
        )
    )
    tenant_feedback = await client.get("/api/v1/feedback?scope=tenant", headers=globex_admin)
    assert tenant_feedback.status_code == 200
    assert tenant_feedback.json()["total"] == 0
    assert "acme-only note" not in tenant_feedback.text


async def test_feedback_memory_never_crosses_tenants(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, acme, acme_cid = await setup_indexed(app, client, demo_db, tenant="acme")
    _, _, globex_cid = await setup_indexed(app, client, demo_db, tenant="globex")
    qid = (await ask(client, acme, acme_cid, REGION_Q)).json()["query_id"]
    assert (await rate(client, acme, qid, 1)).json()["remembered"] is True

    retriever = resources_of(app).retriever
    acme_docs = await retriever.search(
        await scope_of(app, acme_cid), REGION_Q, collections=[Collection.QUERY_FEEDBACK], top_k=10
    )
    globex_docs = await retriever.search(
        await scope_of(app, globex_cid), REGION_Q, collections=[Collection.QUERY_FEEDBACK], top_k=10
    )
    assert [d.doc_key for d in acme_docs] == [f"feedback:{qid}"]
    assert globex_docs == []
    # Even a forged scope (globex tenant, acme connection) finds nothing.
    forged = await scope_of(app, acme_cid)
    globex_tenant = (await scope_of(app, globex_cid)).tenant_id
    forged_docs = await retriever.search(
        type(forged)(
            tenant_id=globex_tenant,
            connection_id=forged.connection_id,
            version=forged.version,
            permissions=forged.permissions,
        ),
        REGION_Q,
        collections=[Collection.QUERY_FEEDBACK],
        top_k=10,
    )
    assert forged_docs == []


async def test_feedback_memory_respects_entity_permissions(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    await rate(client, viewer, qid, 1)
    scope = await scope_of(app, cid)
    no_permissions = type(scope)(
        tenant_id=scope.tenant_id,
        connection_id=scope.connection_id,
        version=scope.version,
        permissions=frozenset(),
    )
    docs = await resources_of(app).retriever.search(
        no_permissions, REGION_Q, collections=[Collection.QUERY_FEEDBACK], top_k=10
    )
    assert docs == []


@pytest.mark.parametrize(
    "search", ["'; DROP TABLE query_history; --", "%' OR '1'='1", "\\", "_%_%_"]
)
async def test_history_search_is_parameterised(
    app: FastAPI,
    client: httpx.AsyncClient,
    demo_db: str,
    sqlite_data_dir: Path,
    search: str,
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    await ask(client, viewer, cid, REGION_Q)
    r = await client.get("/api/v1/history", params={"q": search}, headers=viewer)
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert (await client.get("/api/v1/history", headers=viewer)).json()["total"] == 1
    with sqlite3.connect(sqlite_data_dir / demo_db) as conn:  # ERP data untouched as well
        assert conn.execute("SELECT count(*) FROM sales_orders").fetchone()[0] > 0
