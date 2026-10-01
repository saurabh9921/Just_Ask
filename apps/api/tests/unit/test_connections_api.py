from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import AuditEvent, DatabaseConnection, EncryptedSecret
from tests.conftest import bearer, create_account, login, resources_of

pytestmark = pytest.mark.usefixtures("sqlite_erp")


async def headers_for(
    app: FastAPI, client: httpx.AsyncClient, email: str, roles: list[Role], tenant: str = "acme"
) -> dict[str, str]:
    account = await create_account(app, tenant_slug=tenant, email=email, roles=roles)
    return bearer(await login(client, account))


def pg_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "ERP production",
        "connector_type": "postgresql",
        "host": "erp-db.internal.example",
        "port": 5432,
        "database_name": "erp",
        "schema_name": "analytics",
        "username": "erp_readonly",
        "password": "Sup3r-Secret-DB-Pass",
        "tls_mode": "verify-full",
    }
    body.update(overrides)
    return body


def sqlite_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Demo ERP",
        "connector_type": "sqlite",
        "database_name": "erp.db",
    }
    body.update(overrides)
    return body


async def test_data_admin_creates_connection_and_secret_is_write_only(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post("/api/v1/connections", json=pg_body(), headers=admin)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["read_only"] is True
    assert body["has_secret"] is True
    assert body["secret_source"] == "encrypted"
    assert body["status"] == "untested"
    assert set(body["granted_roles"]) == {"tenant_admin", "data_admin", "analyst", "viewer"}
    assert "Sup3r-Secret-DB-Pass" not in response.text
    assert "secret_ref" not in body and "password" not in body

    async with resources_of(app).database.session_factory() as session:
        conn = await session.scalar(select(DatabaseConnection))
        secret = await session.scalar(select(EncryptedSecret))
    assert conn is not None and secret is not None
    assert conn.secret_ref == f"local://{secret.id}"
    assert b"Sup3r-Secret-DB-Pass" not in secret.ciphertext


async def test_read_only_cannot_be_disabled(app: FastAPI, client: httpx.AsyncClient) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post(
        "/api/v1/connections", json=pg_body(read_only=False), headers=admin
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["body", "read_only"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"connector_type": "mongodb"}, "VALIDATION_ERROR"),
        ({"connector_type": "oracle"}, "CONNECTOR_NOT_SUPPORTED"),
        ({"host": "127.0.0.1"}, "INVALID_CONNECTION_CONFIG"),
        ({"host": "redis"}, "INVALID_CONNECTION_CONFIG"),
        ({"host": "169.254.169.254"}, "INVALID_CONNECTION_CONFIG"),
        ({"host": None}, "INVALID_CONNECTION_CONFIG"),
        ({"max_rows": 5000}, "INVALID_CONNECTION_CONFIG"),
        ({"password": "x", "secret_ref": "env://ERP_SECRET_ACME_PW"}, "VALIDATION_ERROR"),
        ({"password": None, "secret_ref": "env://JWT_SECRET"}, "INVALID_SECRET_REFERENCE"),
        ({"password": None, "secret_ref": "vault://kv/erp"}, "INVALID_SECRET_REFERENCE"),
        ({"pool_min_size": 10, "pool_max_size": 2}, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_connection_configs_rejected(
    app: FastAPI, client: httpx.AsyncClient, overrides: dict[str, Any], code: str
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post("/api/v1/connections", json=pg_body(**overrides), headers=admin)
    assert response.status_code in {403, 422}, response.text
    assert response.json()["error"]["code"] == code


async def test_env_secret_ref_for_own_tenant_accepted(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post(
        "/api/v1/connections",
        json=pg_body(password=None, secret_ref="env://ERP_SECRET_ACME_ERP_PW"),
        headers=admin,
    )
    assert response.status_code == 201
    assert response.json()["secret_source"] == "env"


async def test_insecure_tls_rejected_when_not_allowed(app_factory: Any, settings: Any) -> None:
    app = app_factory(
        settings_override=settings.model_copy(update={"allow_insecure_connection_tls": False})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
        response = await client.post(
            "/api/v1/connections", json=pg_body(tls_mode="disable"), headers=admin
        )
    assert response.json()["error"]["details"]["reason"] == "TLS_REQUIRED"


async def test_duplicate_names_conflict_but_can_be_reused_after_delete(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    first = (await client.post("/api/v1/connections", json=pg_body(), headers=admin)).json()
    dup = await client.post("/api/v1/connections", json=pg_body(), headers=admin)
    assert dup.status_code == 409
    assert (
        await client.delete(f"/api/v1/connections/{first['id']}", headers=admin)
    ).status_code == 204
    again = await client.post("/api/v1/connections", json=pg_body(), headers=admin)
    assert again.status_code == 201
    async with resources_of(app).database.session_factory() as session:
        assert (
            await session.scalar(select(EncryptedSecret).where(EncryptedSecret.id.is_not(None)))
            is not None
        )
        count = len(list(await session.scalars(select(EncryptedSecret))))
    assert count == 1  # the deleted connection's secret was destroyed


async def test_viewer_sees_only_granted_connections_and_cannot_manage(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    viewer = await headers_for(app, client, "v@acme.test", [Role.VIEWER])
    granted = (
        await client.post("/api/v1/connections", json=pg_body(name="Shared"), headers=admin)
    ).json()
    hidden = (
        await client.post(
            "/api/v1/connections",
            json=pg_body(name="Finance only", granted_roles=["data_admin"]),
            headers=admin,
        )
    ).json()

    listed = (await client.get("/api/v1/connections", headers=viewer)).json()
    assert [c["name"] for c in listed["items"]] == ["Shared"]
    assert (
        await client.get(f"/api/v1/connections/{granted['id']}", headers=viewer)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/connections/{hidden['id']}", headers=viewer)
    ).status_code == 404

    create = await client.post("/api/v1/connections", json=pg_body(name="x"), headers=viewer)
    assert create.status_code == 403
    patch = await client.patch(
        f"/api/v1/connections/{granted['id']}", json={"name": "y"}, headers=viewer
    )
    assert patch.status_code == 403
    delete = await client.delete(f"/api/v1/connections/{granted['id']}", headers=viewer)
    assert delete.status_code == 403


async def test_cross_tenant_connection_access_is_404(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    acme = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    globex = await headers_for(app, client, "da@globex.test", [Role.TENANT_ADMIN], tenant="globex")
    conn = (await client.post("/api/v1/connections", json=pg_body(), headers=acme)).json()
    for method, path in [
        ("GET", f"/api/v1/connections/{conn['id']}"),
        ("PATCH", f"/api/v1/connections/{conn['id']}"),
        ("DELETE", f"/api/v1/connections/{conn['id']}"),
        ("POST", f"/api/v1/connections/{conn['id']}/test"),
        ("POST", f"/api/v1/connections/{conn['id']}/metadata/sync"),
        ("GET", f"/api/v1/connections/{conn['id']}/entities"),
    ]:
        response = await client.request(
            method, path, headers=globex, json={} if method == "PATCH" else None
        )
        assert response.status_code == 404, (method, path, response.text)
    assert (await client.get("/api/v1/connections", headers=globex)).json()["total"] == 0


async def test_update_rotates_secret_and_resets_status(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    conn = (await client.post("/api/v1/connections", json=pg_body(), headers=admin)).json()
    async with resources_of(app).database.session_factory() as session:
        old_secret = (await session.scalar(select(EncryptedSecret))).id  # type: ignore[union-attr]
    response = await client.patch(
        f"/api/v1/connections/{conn['id']}",
        json={"password": "N3w-Rotated-Pass", "port": 6543, "granted_roles": ["analyst"]},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["port"] == 6543 and body["granted_roles"] == ["analyst"]
    async with resources_of(app).database.session_factory() as session:
        secrets = list(await session.scalars(select(EncryptedSecret)))
    assert len(secrets) == 1 and secrets[0].id != old_secret


async def test_sqlite_connection_test_succeeds(app: FastAPI, client: httpx.AsyncClient) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    conn = (await client.post("/api/v1/connections", json=sqlite_body(), headers=admin)).json()
    result = await client.post(f"/api/v1/connections/{conn['id']}/test", headers=admin)
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["server_version"].startswith("SQLite")
    refreshed = (await client.get(f"/api/v1/connections/{conn['id']}", headers=admin)).json()
    assert refreshed["status"] == "ok" and refreshed["last_tested_at"] is not None
    async with resources_of(app).database.session_factory() as session:
        events = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "connection.test")
            )
        )
    assert events[0].outcome == "success"


async def test_unreachable_database_reports_error_without_leaking(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    conn = (
        await client.post(
            "/api/v1/connections",
            json=pg_body(host="10.255.255.1", port=1, tls_mode="disable"),
            headers=admin,
        )
    ).json()
    resources_of(app).settings.__dict__["connector_connect_timeout_seconds"] = 0.5
    await resources_of(app).adapters.evict(conn["id"])
    result = await client.post(f"/api/v1/connections/{conn['id']}/test", headers=admin)
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is False
    assert body["error_code"] in {"HOST_UNREACHABLE", "TIMEOUT"}
    assert "Sup3r-Secret-DB-Pass" not in result.text and "erp_readonly" not in result.text


@pytest.mark.parametrize(
    "database_name", ["../app.db", "/etc/passwd", "missing.db", "erp.txt", "..\\..\\x.db"]
)
async def test_sqlite_paths_are_sandboxed(
    app: FastAPI, client: httpx.AsyncClient, database_name: str
) -> None:
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post(
        "/api/v1/connections", json=sqlite_body(database_name=database_name), headers=admin
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CONNECTION_CONFIG"


async def test_sqlite_symlink_escape_rejected(
    app: FastAPI, client: httpx.AsyncClient, sqlite_data_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"")
    link = sqlite_data_dir / "link.db"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this platform")
    admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
    response = await client.post(
        "/api/v1/connections", json=sqlite_body(database_name="link.db"), headers=admin
    )
    assert response.json()["error"]["details"]["reason"] == "PATH_NOT_ALLOWED"


async def test_sqlite_disabled_in_production_like_settings(app_factory: Any, settings: Any) -> None:
    app = app_factory(
        settings_override=settings.model_copy(update={"sqlite_connections_enabled": False})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        admin = await headers_for(app, client, "da@acme.test", [Role.DATA_ADMIN])
        response = await client.post("/api/v1/connections", json=sqlite_body(), headers=admin)
    assert response.json()["error"]["details"]["reason"] == "SQLITE_DISABLED"
