"""Phase 2 security regression tests: token misuse, tenant isolation, secrets, SSRF."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import AuditEvent, DatabaseConnection
from tests.conftest import TEST_EXTENSION_ORIGIN, bearer, create_account, login, resources_of

pytestmark = pytest.mark.security

PG = {
    "connector_type": "postgresql",
    "host": "erp.example.internal",
    "database_name": "erp",
    "username": "ro",
    "password": "Very-Secret-ERP-Password",
}


# ---------------------------------------------------------------- token misuse
async def test_token_with_swapped_tenant_claim_is_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    victim = await create_account(
        app, tenant_slug="victim", email="v@victim.test", roles=[Role.TENANT_ADMIN]
    )
    attacker = await create_account(
        app, tenant_slug="evil", email="a@evil.test", roles=[Role.TENANT_ADMIN]
    )
    tokens = await login(client, attacker)
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    claims["tid"] = str(victim.tenant_id)
    forged = jwt.encode(claims, "guessed-secret-" + "x" * 32, algorithm="HS256")
    r = await client.get("/api/v1/connections", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


async def test_validly_signed_token_for_foreign_tenant_is_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Even with the signing key, a user cannot be moved into another tenant."""
    victim = await create_account(
        app, tenant_slug="victim", email="v@victim.test", roles=[Role.VIEWER]
    )
    attacker = await create_account(
        app, tenant_slug="evil", email="a@evil.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, attacker)
    claims = resources_of(app).tokens.decode_access_token(tokens["access_token"])
    token, _ = resources_of(app).tokens.issue_access_token(
        user_id=claims.user_id, tenant_id=victim.tenant_id, session_id=claims.session_id
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_token_bound_to_unknown_session_is_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    user = await create_account(app, tenant_slug="acme", email="u@acme.test", roles=[Role.VIEWER])
    token, _ = resources_of(app).tokens.issue_access_token(
        user_id=user.user_id, tenant_id=user.tenant_id, session_id=uuid.uuid4()
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["error"]["code"] == "SESSION_REVOKED"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "bearer not.a.jwt",
        "Bearer " + "a" * 5000,
        "Token abc",
    ],
)
async def test_malformed_authorization_headers(client: httpx.AsyncClient, header: str) -> None:
    r = await client.get("/api/v1/connections", headers={"Authorization": header})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_refresh_token_cannot_be_used_as_access_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    user = await create_account(app, tenant_slug="acme", email="u@acme.test", roles=[Role.VIEWER])
    tokens = await login(client, user)
    r = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert r.status_code == 401


async def test_access_token_cannot_be_used_as_refresh_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    user = await create_account(app, tenant_slug="acme", email="u@acme.test", roles=[Role.VIEWER])
    tokens = await login(client, user)
    r = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"][:200]}
    )
    assert r.status_code == 401


# ------------------------------------------------------------ user enumeration
async def test_login_errors_do_not_reveal_which_part_was_wrong(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_account(app, tenant_slug="acme", email="real@acme.test", roles=[Role.VIEWER])
    attempts = [
        {"tenant": "nope", "email": "real@acme.test", "password": "whatever-123"},
        {"tenant": "acme", "email": "ghost@acme.test", "password": "whatever-123"},
        {"tenant": "acme", "email": "real@acme.test", "password": "whatever-123"},
    ]
    bodies = []
    for attempt in attempts:
        r = await client.post("/api/v1/auth/login", json=attempt)
        assert r.status_code == 401
        error = r.json()["error"]
        bodies.append((error["code"], error["message"], error["details"]))
    assert len(set(map(str, bodies))) == 1


# ------------------------------------------------------------- secret handling
async def test_secrets_never_appear_in_responses_logs_or_audit(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    admin = await create_account(
        app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
    )
    headers = bearer(await login(client, admin))
    created = await client.post("/api/v1/connections", json={"name": "c", **PG}, headers=headers)
    conn_id = created.json()["id"]
    listing = await client.get("/api/v1/connections", headers=headers)
    patched = await client.patch(
        f"/api/v1/connections/{conn_id}", json={"password": "Rotated-Secret-XYZ"}, headers=headers
    )
    for response in (created, listing, patched):
        assert "Very-Secret-ERP-Password" not in response.text
        assert "Rotated-Secret-XYZ" not in response.text
        assert "local://" not in response.text

    all_logs = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in ("Very-Secret-ERP-Password", "Rotated-Secret-XYZ", admin.password):
        assert secret not in all_logs

    async with resources_of(app).database.session_factory() as session:
        details = [str(e.details) for e in await session.scalars(select(AuditEvent))]
    assert details and not any("Secret" in d for d in details)


async def test_env_secret_of_another_tenant_cannot_be_referenced(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await create_account(
        app, tenant_slug="evil", email="da@evil.test", roles=[Role.DATA_ADMIN]
    )
    headers = bearer(await login(client, admin))
    body = {**PG, "name": "steal", "password": None, "secret_ref": "env://ERP_SECRET_ACME_ERP_PW"}
    r = await client.post("/api/v1/connections", json=body, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "ENV_NAME_NOT_ALLOWED"


async def test_connection_rows_store_references_not_passwords(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await create_account(
        app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
    )
    headers = bearer(await login(client, admin))
    await client.post("/api/v1/connections", json={"name": "c", **PG}, headers=headers)
    async with resources_of(app).database.session_factory() as session:
        conn = await session.scalar(select(DatabaseConnection))
    assert conn is not None
    for value in vars(conn).values():
        assert "Very-Secret-ERP-Password" not in str(value)


# -------------------------------------------------------------- SSRF / infra
@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "metadata.google.internal",
        "redis",
        "qdrant",
        "::1",
    ],
)
async def test_platform_infrastructure_is_not_reachable_via_connections(
    app: FastAPI, client: httpx.AsyncClient, host: str
) -> None:
    admin = await create_account(
        app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
    )
    headers = bearer(await login(client, admin))
    r = await client.post(
        "/api/v1/connections", json={"name": "probe", **PG, "host": host}, headers=headers
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "HOST_NOT_ALLOWED"


async def test_injection_payloads_in_names_are_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = await create_account(
        app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
    )
    headers = bearer(await login(client, admin))
    for name in ["x'; DROP TABLE users; --", "<script>alert(1)</script>", "a\nb"]:
        r = await client.post("/api/v1/connections", json={**PG, "name": name}, headers=headers)
        assert r.status_code == 422, name


# ---------------------------------------------------------------------- CORS
async def test_extension_preflight_allows_authorization_header(client: httpx.AsyncClient) -> None:
    r = await client.options(
        "/api/v1/auth/me",
        headers={
            "Origin": TEST_EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-request-id",
        },
    )
    assert r.status_code == 200
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


def test_access_tokens_are_short_lived(app: FastAPI) -> None:
    res = resources_of(app)
    _, expires = res.tokens.issue_access_token(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert (expires - datetime.now(tz=UTC)).total_seconds() <= 3600
