from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.permissions import Role
from tests.conftest import Account, bearer, create_account, login


async def _admin(
    app: FastAPI, client: httpx.AsyncClient, role: Role = Role.TENANT_ADMIN
) -> tuple[Account, dict[str, str]]:
    account = await create_account(app, tenant_slug="acme", email="boss@acme.test", roles=[role])
    return account, bearer(await login(client, account))


def new_user(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "email": "new@acme.test",
        "display_name": "New User",
        "password": "Long-Enough-Passphrase-1",
        "roles": ["analyst"],
    }
    body.update(overrides)
    return body


async def test_tenant_admin_creates_user_who_can_login(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _, headers = await _admin(app, client)
    created = await client.post("/api/v1/admin/users", json=new_user(), headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["roles"] == ["analyst"]
    assert "Long-Enough-Passphrase-1" not in created.text
    ok = await client.post(
        "/api/v1/auth/login",
        json={"tenant": "acme", "email": "new@acme.test", "password": "Long-Enough-Passphrase-1"},
    )
    assert ok.status_code == 200
    listed = (await client.get("/api/v1/admin/users", headers=headers)).json()
    assert listed["total"] == 2


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (new_user(password="short"), 400),
        (new_user(email="jordan@acme.test", password="Jordan-Password-123"), 400),  # has local part
        (new_user(roles=[]), 422),
        (new_user(roles=["superuser"]), 422),
    ],
)
async def test_user_validation(
    app: FastAPI, client: httpx.AsyncClient, body: dict[str, object], status: int
) -> None:
    _, headers = await _admin(app, client)
    assert (
        await client.post("/api/v1/admin/users", json=body, headers=headers)
    ).status_code == status


async def test_no_privilege_escalation(app: FastAPI, client: httpx.AsyncClient) -> None:
    _, headers = await _admin(app, client)
    r = await client.post(
        "/api/v1/admin/users", json=new_user(roles=["platform_admin"]), headers=headers
    )
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == "CANNOT_GRANT_ROLE"


async def test_analyst_cannot_administer(app: FastAPI, client: httpx.AsyncClient) -> None:
    _, headers = await _admin(app, client, role=Role.ANALYST)
    assert (await client.get("/api/v1/admin/users", headers=headers)).status_code == 403
    assert (
        await client.post("/api/v1/admin/users", json=new_user(), headers=headers)
    ).status_code == 403


async def test_self_lockout_prevented(app: FastAPI, client: httpx.AsyncClient) -> None:
    admin, headers = await _admin(app, client)
    r1 = await client.patch(
        f"/api/v1/admin/users/{admin.user_id}", json={"is_active": False}, headers=headers
    )
    r2 = await client.patch(
        f"/api/v1/admin/users/{admin.user_id}", json={"roles": ["viewer"]}, headers=headers
    )
    assert r1.status_code == 400 and r2.status_code == 400


async def test_deactivation_takes_effect_immediately(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _, headers = await _admin(app, client)
    victim = await create_account(
        app, tenant_slug="acme", email="leaver@acme.test", roles=[Role.ANALYST]
    )
    victim_tokens = await login(client, victim)
    r = await client.patch(
        f"/api/v1/admin/users/{victim.user_id}", json={"is_active": False}, headers=headers
    )
    assert r.status_code == 200
    me = await client.get("/api/v1/auth/me", headers=bearer(victim_tokens))
    assert me.json()["error"]["code"] == "SESSION_REVOKED"


async def test_role_change_applies_on_next_request(app: FastAPI, client: httpx.AsyncClient) -> None:
    _, headers = await _admin(app, client)
    user = await create_account(
        app, tenant_slug="acme", email="promo@acme.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, user)
    before = await client.post(
        "/api/v1/connections",
        json={
            "name": "x",
            "connector_type": "postgresql",
            "host": "db.example",
            "database_name": "d",
            "username": "u",
        },
        headers=bearer(tokens),
    )
    assert before.status_code == 403
    await client.patch(
        f"/api/v1/admin/users/{user.user_id}", json={"roles": ["data_admin"]}, headers=headers
    )
    me = (await client.get("/api/v1/auth/me", headers=bearer(tokens))).json()
    assert "connections.manage" in me["permissions"]


async def test_admin_cannot_touch_other_tenant_users(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _, headers = await _admin(app, client)
    other = await create_account(
        app, tenant_slug="globex", email="x@globex.test", roles=[Role.VIEWER]
    )
    r = await client.patch(
        f"/api/v1/admin/users/{other.user_id}", json={"is_active": False}, headers=headers
    )
    assert r.status_code == 404
