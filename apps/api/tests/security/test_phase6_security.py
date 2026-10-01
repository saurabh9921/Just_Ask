"""Phase 6 security tests: result-cache isolation, export isolation and file safety,
connection-string injection and replica host policy."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.core.permissions import Role
from app.db.models import ExportJob
from app.services.export_service import export_path
from tests.conftest import bearer, create_account, login, make_settings, resources_of
from tests.fixtures.demo_erp import seed_demo
from tests.unit.test_analytics_api import REGION_Q, ask, fixed_today
from tests.unit.test_exports import export, finished
from tests.unit.test_query_engine import clone_tenant
from tests.unit.test_semantic_api import setup_indexed

pytestmark = pytest.mark.security
__all__ = ["fixed_today"]


@pytest.fixture
async def demo_db(sqlite_data_dir: Path) -> str:
    path = sqlite_data_dir / "demo.db"
    await seed_demo(
        f"sqlite+aiosqlite:///{path.as_posix()}", anchor_date=date(2026, 9, 15), tenant_code="acme"
    )
    clone_tenant(path, "globex")
    with sqlite3.connect(path) as conn:  # different totals per tenant
        conn.execute("DELETE FROM sales_orders WHERE tenant_code = 'globex' AND order_id % 2 = 0")
    return "demo.db"


async def analyst(app: FastAPI, client: httpx.AsyncClient, tenant: str) -> dict[str, str]:
    return bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug=tenant, email=f"an@{tenant}.test", roles=[Role.ANALYST]
            ),
        )
    )


def export_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def totals(body: dict[str, Any]) -> dict[str, float]:
    return {row[0]: row[1] for row in body["result"]["rows"]}


async def test_result_cache_never_crosses_tenants(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    """Same question, same database, same plan: each tenant still gets its own rows."""
    _, acme, acme_cid = await setup_indexed(app, client, demo_db, tenant="acme")
    _, globex, globex_cid = await setup_indexed(app, client, demo_db, tenant="globex")
    first_acme = (await ask(client, acme, acme_cid, REGION_Q)).json()
    first_globex = (await ask(client, globex, globex_cid, REGION_Q)).json()
    assert first_globex["cache"]["hit"] is False
    assert totals(first_acme) != totals(first_globex)
    again_acme = (await ask(client, acme, acme_cid, REGION_Q)).json()
    again_globex = (await ask(client, globex, globex_cid, REGION_Q)).json()
    assert again_acme["cache"]["hit"] and again_globex["cache"]["hit"]
    assert totals(again_acme) == totals(first_acme)
    assert totals(again_globex) == totals(first_globex)
    keys = [k async for k in resources_of(app).redis.scan_iter(match="qr:*")]
    tenants = {k.decode().split(":")[1] for k in keys}
    assert len(tenants) == 2


async def test_exports_are_tenant_and_user_isolated(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, acme_cid = await setup_indexed(app, client, demo_db, tenant="acme")
    await setup_indexed(app, client, demo_db, tenant="globex")
    acme = await analyst(app, client, "acme")
    globex = await analyst(app, client, "globex")
    qid = (await ask(client, acme, acme_cid, REGION_Q)).json()["query_id"]
    assert (await export(client, globex, qid)).status_code == 404
    job = await finished(app, client, acme, (await export(client, acme, qid)).json()["export_id"])
    eid = job["export_id"]
    for method, path in (
        ("GET", f"/api/v1/exports/{eid}"),
        ("GET", f"/api/v1/exports/{eid}/file"),
        ("POST", f"/api/v1/exports/{eid}/download-token"),
        ("DELETE", f"/api/v1/exports/{eid}"),
    ):
        assert (await client.request(method, path, headers=globex)).status_code == 404, path
    assert (await client.get("/api/v1/exports", headers=globex)).json()["items"] == []
    files = export_files(Path(resources_of(app).settings.export_dir))
    async with resources_of(app).database.session_factory() as session:
        stored = await session.scalar(select(ExportJob))
    assert stored is not None
    assert len(files) == 1 and files[0].parent.name == str(stored.tenant_id)


@pytest.mark.parametrize(
    "key", ["../../etc/passwd", "..\\secret.csv", "abc.csv", "0" * 32 + ".exe", None]
)
def test_export_paths_cannot_escape_the_export_directory(tmp_path: Path, key: str | None) -> None:
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", tmp_path)
    job = ExportJob(tenant_id=uuid.uuid4(), storage_key=key)
    with pytest.raises(NotFoundError):
        export_path(settings, job)


async def test_download_tokens_are_unguessable_and_not_bearer_tokens(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    acme = await analyst(app, client, "acme")
    qid = (await ask(client, acme, cid, REGION_Q)).json()["query_id"]
    job = await finished(app, client, acme, (await export(client, acme, qid)).json()["export_id"])
    # A bearer access token is not a download token.
    token = acme["Authorization"].removeprefix("Bearer ")
    assert (await client.get(f"/api/v1/exports/download/{token}")).status_code == 404
    minted = (
        await client.post(f"/api/v1/exports/{job['export_id']}/download-token", headers=acme)
    ).json()
    assert len(minted["url"].rsplit("/", 1)[1]) >= 40
    stored = [k async for k in resources_of(app).redis.scan_iter(match="export:dl:*")]
    assert len(stored) == 1 and minted["url"].rsplit("/", 1)[1].encode() not in stored[0]  # hashed


async def test_export_job_reapplies_tenant_row_filters(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, sqlite_data_dir: Path
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db, tenant="acme")
    acme = await analyst(app, client, "acme")
    body = (await ask(client, acme, cid, REGION_Q)).json()
    job = await finished(
        app,
        client,
        acme,
        (await export(client, acme, body["query_id"], scope="all_rows")).json()["export_id"],
    )
    token = (
        await client.post(f"/api/v1/exports/{job['export_id']}/download-token", headers=acme)
    ).json()
    content = (await client.get(token["url"])).content.decode("utf-8-sig")
    exported = {
        line.split(",")[0]: float(line.split(",")[1]) for line in content.strip().splitlines()[1:]
    }
    assert exported == pytest.approx(totals(body))  # acme's rows only, not acme + globex


@pytest.mark.parametrize(
    "field",
    [
        {"host": "db.example.com;Trusted_Connection=yes"},
        {"host": "db.example.com", "read_replica_host": "127.0.0.1"},
        {"host": "db.example.com", "read_replica_host": "169.254.169.254"},
        {"host": "db.example.com", "read_replica_port": 1444},
    ],
)
async def test_connection_fields_cannot_inject_or_reach_internal_hosts(
    app: FastAPI, client: httpx.AsyncClient, field: dict[str, Any]
) -> None:
    admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
            ),
        )
    )
    body = {
        "name": "ERP SQL Server",
        "connector_type": "sqlserver",
        "database_name": "erp",
        "username": "reader",
        **field,
    }
    r = await client.post("/api/v1/connections", json=body, headers=admin)
    assert r.status_code == 422, r.text
