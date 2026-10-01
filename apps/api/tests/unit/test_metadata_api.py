from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.permissions import Role
from app.core.security import utcnow
from app.db.models import MetadataSnapshot
from app.services import metadata_service
from app.workers.jobs import STALE_JOB_AFTER
from app.workers.queues import DEFAULT_HARD_TIME_LIMIT_SECONDS
from tests.conftest import bearer, create_account, login, resources_of


async def _setup(
    app: FastAPI, client: httpx.AsyncClient
) -> tuple[dict[str, str], dict[str, str], str]:
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
    conn = await client.post(
        "/api/v1/connections",
        json={"name": "Demo", "connector_type": "sqlite", "database_name": "erp.db"},
        headers=admin,
    )
    assert conn.status_code == 201, conn.text
    return admin, viewer, conn.json()["id"]


async def _sync(
    app: FastAPI, client: httpx.AsyncClient, headers: dict[str, str], conn_id: str
) -> dict[str, Any]:
    response = await client.post(f"/api/v1/connections/{conn_id}/metadata/sync", headers=headers)
    assert response.status_code == 202, response.text
    assert response.headers["location"].endswith("/sync-status")
    await resources_of(app).metadata_jobs.wait_idle()
    status = await client.get(f"/api/v1/connections/{conn_id}/sync-status", headers=headers)
    body: dict[str, Any] = status.json()
    return body


async def test_sync_discovers_tables_views_and_keys(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, viewer, conn_id = await _setup(app, client)
    status = await _sync(app, client, admin, conn_id)
    latest = status["latest"]
    assert latest["status"] == "succeeded", latest
    assert latest["version"] == 1
    assert latest["object_count"] == 4
    assert latest["added_count"] == 4 and latest["changed_count"] == 0
    assert status["active_version"] == 1

    entities = (await client.get(f"/api/v1/connections/{conn_id}/entities", headers=viewer)).json()
    assert entities["total"] == 4
    kinds = {e["name"]: e["kind"] for e in entities["items"]}
    assert kinds == {
        "analytics_sales_orders": "view",
        "customers": "table",
        "regions": "table",
        "sales_orders": "table",
    }

    detail = (
        await client.get(
            f"/api/v1/connections/{conn_id}/entities/main.sales_orders", headers=viewer
        )
    ).json()
    assert detail["primary_key"] == ["order_id"]
    assert detail["foreign_keys"][0]["referred_table"] == "customers"
    assert {c["name"]: c["canonical_type"] for c in detail["columns"]}["net_amount"] == "decimal"
    unqualified = await client.get(
        f"/api/v1/connections/{conn_id}/entities/sales_orders", headers=viewer
    )
    assert unqualified.status_code == 200


async def test_resync_reports_changes(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, _, conn_id = await _setup(app, client)
    await _sync(app, client, admin, conn_id)
    db = sqlite3.connect(sqlite_erp)
    db.executescript(
        "ALTER TABLE customers ADD COLUMN segment TEXT; DROP VIEW analytics_sales_orders;"
        "CREATE TABLE products (product_id INTEGER PRIMARY KEY, name TEXT);"
    )
    db.close()
    status = await _sync(app, client, admin, conn_id)
    latest = status["latest"]
    assert latest["version"] == 2
    assert (latest["added_count"], latest["changed_count"], latest["removed_count"]) == (1, 1, 1)


async def test_search_and_pagination(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, viewer, conn_id = await _setup(app, client)
    await _sync(app, client, admin, conn_id)
    found = (
        await client.get(f"/api/v1/connections/{conn_id}/entities?search=SALES", headers=viewer)
    ).json()
    assert {e["name"] for e in found["items"]} == {"sales_orders", "analytics_sales_orders"}
    # LIKE wildcards in user input are escaped, not interpreted
    none = (
        await client.get(f"/api/v1/connections/{conn_id}/entities?search=%25", headers=viewer)
    ).json()
    assert none["total"] == 0
    page = (
        await client.get(f"/api/v1/connections/{conn_id}/entities?limit=2&offset=2", headers=viewer)
    ).json()
    assert len(page["items"]) == 2 and page["total"] == 4


async def test_failed_sync_records_error_code(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, _, conn_id = await _setup(app, client)
    sqlite_erp.unlink()  # noqa: ASYNC240 - test setup; file vanishes after creation
    status = await _sync(app, client, admin, conn_id)
    assert status["latest"]["status"] == "failed"
    assert status["latest"]["error_code"] == "FILE_NOT_FOUND"
    assert status["active_version"] is None


async def test_viewer_cannot_trigger_sync_and_unknown_entity_is_404(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, viewer, conn_id = await _setup(app, client)
    denied = await client.post(f"/api/v1/connections/{conn_id}/metadata/sync", headers=viewer)
    assert denied.status_code == 403
    empty = (await client.get(f"/api/v1/connections/{conn_id}/entities", headers=viewer)).json()
    assert empty == {"items": [], "total": 0, "version": None, "synced_at": None}
    await _sync(app, client, admin, conn_id)
    missing = await client.get(f"/api/v1/connections/{conn_id}/entities/nope", headers=viewer)
    assert missing.status_code == 404


async def test_concurrent_sync_requests_share_one_snapshot(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, _, conn_id = await _setup(app, client)
    first = (
        await client.post(f"/api/v1/connections/{conn_id}/metadata/sync", headers=admin)
    ).json()
    second = (
        await client.post(f"/api/v1/connections/{conn_id}/metadata/sync", headers=admin)
    ).json()
    await resources_of(app).metadata_jobs.wait_idle()
    assert (
        first["snapshot_id"] == second["snapshot_id"] or second["version"] == first["version"] + 1
    )


async def test_unexpected_sync_error_ends_failed_and_does_not_block_resync(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-AppError (e.g. a raw driver exception) used to leave the snapshot "running"
    forever, and every later sync request returned that dead snapshot."""
    admin, _, conn_id = await _setup(app, client)

    async def crash(*_args: object) -> None:
        raise RuntimeError("driver blew up")

    monkeypatch.setattr(metadata_service, "_discover", crash)
    failed = await _sync(app, client, admin, conn_id)
    assert failed["latest"]["status"] == "failed"
    assert failed["latest"]["error_code"] == "UNKNOWN"

    monkeypatch.undo()
    retried = await _sync(app, client, admin, conn_id)
    assert retried["latest"]["status"] == "succeeded"
    assert retried["latest"]["version"] == 2


async def test_abandoned_sync_is_superseded(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    assert timedelta(seconds=DEFAULT_HARD_TIME_LIMIT_SECONDS) < STALE_JOB_AFTER
    admin, _, conn_id = await _setup(app, client)
    await _sync(app, client, admin, conn_id)
    orphan_id = uuid.uuid4()
    async with resources_of(app).database.session_factory() as session:
        first = await session.get(
            MetadataSnapshot,
            uuid.UUID(
                (
                    await client.get(f"/api/v1/connections/{conn_id}/sync-status", headers=admin)
                ).json()["latest"]["snapshot_id"]
            ),
        )
        assert first is not None
        started = utcnow() - STALE_JOB_AFTER - timedelta(minutes=1)
        session.add(
            MetadataSnapshot(
                id=orphan_id,
                tenant_id=first.tenant_id,
                connection_id=first.connection_id,
                version=2,
                status="running",
                created_at=started,
                started_at=started,
            )
        )
        await session.commit()

    status = await _sync(app, client, admin, conn_id)
    assert status["latest"]["status"] == "succeeded"
    assert status["latest"]["version"] == 3
    async with resources_of(app).database.session_factory() as session:
        orphan = await session.get(MetadataSnapshot, orphan_id)
        assert orphan is not None
        assert (orphan.status, orphan.error_code) == ("failed", "ABANDONED")
        assert orphan.finished_at is not None


async def test_recent_in_flight_sync_is_not_superseded(
    app: FastAPI, client: httpx.AsyncClient, sqlite_erp: Path
) -> None:
    admin, _, conn_id = await _setup(app, client)
    await _sync(app, client, admin, conn_id)
    snapshot_id = (
        await client.get(f"/api/v1/connections/{conn_id}/sync-status", headers=admin)
    ).json()["latest"]["snapshot_id"]
    async with resources_of(app).database.session_factory() as session:
        row = await session.get(MetadataSnapshot, uuid.UUID(snapshot_id))
        assert row is not None
        row.status, row.started_at = "running", utcnow()
        await session.commit()
    again = await client.post(f"/api/v1/connections/{conn_id}/metadata/sync", headers=admin)
    assert again.json()["snapshot_id"] == snapshot_id
