from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.permissions import Role
from app.db.models import AuditEvent, RefreshToken, User
from tests.conftest import (
    AppFactory,
    bearer,
    client_for,
    create_account,
    login,
    resources_of,
)


async def _events(app: FastAPI, event_type: str) -> list[AuditEvent]:
    async with resources_of(app).database.session_factory() as session:
        return list(
            await session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))
        )


async def test_login_returns_tokens_and_profile(app: FastAPI, client: httpx.AsyncClient) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="ana@acme.test", roles=[Role.ANALYST]
    )
    body = await login(client, account)
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 600
    assert body["refresh_expires_in"] > 3600
    user = body["user"]
    assert user["email"] == "ana@acme.test"
    assert user["tenant"]["slug"] == "acme"
    assert user["roles"] == ["analyst"]
    assert "analytics.query" in user["permissions"]
    assert "admin.manage" not in user["permissions"]
    (event,) = await _events(app, "auth.login")
    assert event.outcome == "success"
    assert event.actor_user_id == account.user_id


async def test_login_is_case_insensitive_for_email_and_tenant(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_account(app, tenant_slug="acme", email="ana@acme.test", roles=[Role.VIEWER])
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant": " ACME ", "email": "Ana@Acme.Test", "password": "Correct-Horse-Battery-9"},
    )
    assert response.status_code == 200


async def test_me_requires_and_accepts_bearer_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="me@acme.test", roles=[Role.VIEWER]
    )
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    tokens = await login(client, account)
    response = await client.get("/api/v1/auth/me", headers=bearer(tokens))
    assert response.status_code == 200
    assert response.json()["id"] == str(account.user_id)


async def test_refresh_rotates_and_old_token_is_single_use(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="r@acme.test", roles=[Role.VIEWER]
    )
    first = await login(client, account)
    second = (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    # session lifetime is absolute: rotation does not extend it
    assert second["refresh_expires_in"] <= first["refresh_expires_in"]
    assert (await client.get("/api/v1/auth/me", headers=bearer(second))).status_code == 200


async def test_refresh_token_reuse_revokes_the_whole_session(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="reuse@acme.test", roles=[Role.VIEWER]
    )
    first = await login(client, account)
    second = (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()

    # Attacker replays the stolen, already-rotated token.
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "REFRESH_TOKEN_INVALID"

    # The legitimate client's newer tokens are dead too.
    me = await client.get("/api/v1/auth/me", headers=bearer(second))
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "SESSION_REVOKED"
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert refresh.status_code == 401
    reuse_events = await _events(app, "auth.refresh_reuse")
    assert reuse_events and all(e.outcome == "denied" for e in reuse_events)


async def test_logout_revokes_access_immediately(app: FastAPI, client: httpx.AsyncClient) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="out@acme.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, account)
    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 204
    me = await client.get("/api/v1/auth/me", headers=bearer(tokens))
    assert me.json()["error"]["code"] == "SESSION_REVOKED"
    again = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert again.status_code == 401


async def test_logout_with_unknown_token_is_still_204(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": "x" * 43})
    assert response.status_code == 204


async def test_expired_access_token_reports_token_expired(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="exp@acme.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, account)
    res = resources_of(app)
    claims = res.tokens.decode_access_token(tokens["access_token"])
    old, _ = res.tokens.issue_access_token(
        user_id=claims.user_id,
        tenant_id=claims.tenant_id,
        session_id=claims.session_id,
        now=datetime.now(tz=UTC) - timedelta(hours=2),
    )
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_EXPIRED"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_wrong_password_increments_and_locks(app: FastAPI, client: httpx.AsyncClient) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="lock@acme.test", roles=[Role.VIEWER]
    )
    threshold = resources_of(app).settings.login_lockout_threshold
    for _ in range(threshold):
        response = await client.post(
            "/api/v1/auth/login",
            json={"tenant": "acme", "email": account.email, "password": "wrong-password-1"},
        )
        assert response.json()["error"]["code"] in {"INVALID_CREDENTIALS", "RATE_LIMITED"}

    async with resources_of(app).database.session_factory() as session:
        user = await session.get(User, account.user_id)
        assert user is not None
        assert user.failed_login_count == threshold
        assert user.locked_until is not None

    # Correct password is refused while locked - with the same generic error.
    await resources_of(app).redis.flushall()  # isolate from the per-account rate limit
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant": "acme", "email": account.email, "password": account.password},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_successful_login_resets_failure_counter(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="reset@acme.test", roles=[Role.VIEWER]
    )
    await client.post(
        "/api/v1/auth/login",
        json={"tenant": "acme", "email": account.email, "password": "nope-nope-1"},
    )
    await login(client, account)
    async with resources_of(app).database.session_factory() as session:
        user = await session.get(User, account.user_id)
        assert user is not None and user.failed_login_count == 0


async def test_per_account_rate_limit_returns_429_with_retry_after(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_account(app, tenant_slug="acme", email="rl@acme.test", roles=[Role.VIEWER])
    limit = resources_of(app).settings.login_rate_limit_per_account_per_minute
    statuses = []
    for _ in range(limit + 1):
        r = await client.post(
            "/api/v1/auth/login",
            json={"tenant": "acme", "email": "rl@acme.test", "password": "bad-password-x"},
        )
        statuses.append(r.status_code)
    assert statuses[-1] == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "RATE_LIMITED"


async def test_inactive_user_cannot_login(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_account(
        app, tenant_slug="acme", email="off@acme.test", roles=[Role.VIEWER], active=False
    )
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant": "acme", "email": "off@acme.test", "password": "Correct-Horse-Battery-9"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_only_token_hashes_are_stored(app: FastAPI, client: httpx.AsyncClient) -> None:
    account = await create_account(
        app, tenant_slug="acme", email="hash@acme.test", roles=[Role.VIEWER]
    )
    tokens = await login(client, account)
    async with resources_of(app).database.session_factory() as session:
        stored = list(await session.scalars(select(RefreshToken.token_hash)))
    assert tokens["refresh_token"] not in stored
    assert all(len(h) == 64 for h in stored)


async def test_login_is_audited_without_credentials(app_factory: AppFactory) -> None:
    app = app_factory()
    await create_account(app, tenant_slug="acme", email="aud@acme.test", roles=[Role.VIEWER])
    async with client_for(app) as client:
        await client.post(
            "/api/v1/auth/login",
            json={"tenant": "acme", "email": "ghost@acme.test", "password": "Secret-Guess-123"},
        )
    (event,) = await _events(app, "auth.login")
    assert event.outcome == "denied"
    assert event.details["reason"] == "unknown_account"
    serialized = str(event.details)
    assert "Secret-Guess-123" not in serialized and "ghost@acme.test" not in serialized
