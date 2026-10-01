"""Authentication endpoints: login, refresh (rotation), logout, me."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from app.core.exceptions import AuthenticationError
from app.db.models import Tenant, User
from app.dependencies import DbSessionDep, PrincipalDep, ResourcesDep, client_ip
from app.schemas.auth import LoginRequest, LogoutRequest, MeResponse, RefreshRequest, TokenResponse
from app.services.auth_service import AuthService, build_me

router = APIRouter(prefix="/auth", tags=["auth"])


def _service(resources: ResourcesDep, session: DbSessionDep) -> AuthService:
    return AuthService(
        session=session,
        settings=resources.settings,
        passwords=resources.passwords,
        tokens=resources.tokens,
        rate_limiter=resources.rate_limiter,
        audit=resources.audit,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, resources: ResourcesDep, session: DbSessionDep
) -> TokenResponse:
    return await _service(resources, session).login(
        tenant_slug=body.tenant,
        email=body.email,
        password=body.password.get_secret_value(),
        client_ip=client_ip(request),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, resources: ResourcesDep, session: DbSessionDep
) -> TokenResponse:
    return await _service(resources, session).refresh(body.refresh_token.get_secret_value())


@router.post("/logout", status_code=204, response_class=Response)
async def logout(body: LogoutRequest, resources: ResourcesDep, session: DbSessionDep) -> Response:
    await _service(resources, session).logout(body.refresh_token.get_secret_value())
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
async def me(ctx: PrincipalDep, session: DbSessionDep) -> MeResponse:
    row = (
        await session.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(User.id == ctx.user_id, User.tenant_id == ctx.tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise AuthenticationError()
    return await build_me(session, row[0], row[1])
