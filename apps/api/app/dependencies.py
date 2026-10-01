"""FastAPI dependency providers.

Long-lived resources are created once in the application lifespan and stored on
``app.state.resources``; request handlers obtain them through these providers,
which keeps handlers testable via ``app.dependency_overrides``.

Authentication: ``get_principal`` validates the bearer token, re-loads the user,
tenant and session state from the database on every request (so deactivation,
logout and refresh-token reuse take effect immediately), and binds a
``TenantContext`` for the rest of the request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import Depends, Request
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.common.network import HostPolicy
from app.connectors.factory import AdapterCache
from app.core.exceptions import AuthenticationError
from app.core.permissions import Permission, Role
from app.core.rate_limit import RateLimiter
from app.core.security import InvalidTokenError, PasswordService, TokenService
from app.core.tenant_context import TenantContext, bind_context
from app.db.models import RefreshToken, Tenant, User, UserRole
from app.db.models import Role as RoleModel
from app.db.session import Database
from app.llm.base import LLMProvider
from app.query.result_cache import CatalogCache, QueryResultCache
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.ingestion import IngestionDeps
from app.rag.qdrant_client import VectorStore
from app.semantic.registry import SemanticLayerRegistry
from app.services.analytics_service import QueryRunRegistry
from app.services.audit_service import AuditService
from app.services.health_service import HealthService
from app.services.secrets_service import SecretCipher
from app.workers.jobs import JobRunner


class SessionRevokedError(AuthenticationError):
    code = "SESSION_REVOKED"
    default_message = "The session has ended. Please sign in again."


@dataclass(slots=True)
class AppResources:
    settings: Settings
    database: Database
    redis: Redis
    http: httpx.AsyncClient
    health: HealthService
    passwords: PasswordService
    tokens: TokenService
    rate_limiter: RateLimiter
    cipher: SecretCipher
    adapters: AdapterCache
    host_policy: HostPolicy
    audit: AuditService
    metadata_jobs: JobRunner
    qdrant: AsyncQdrantClient
    vector_store: VectorStore
    retriever: HybridRetriever
    llm: LLMProvider
    semantic_registry: SemanticLayerRegistry
    semantic_jobs: JobRunner
    analytics_runs: QueryRunRegistry
    ingestion: IngestionDeps
    result_cache: QueryResultCache
    catalog_cache: CatalogCache
    export_jobs: JobRunner


def get_resources(request: Request) -> AppResources:
    resources: AppResources = request.app.state.resources
    return resources


ResourcesDep = Annotated[AppResources, Depends(get_resources)]


def get_app_settings(resources: ResourcesDep) -> Settings:
    return resources.settings


def get_health_service(resources: ResourcesDep) -> HealthService:
    return resources.health


async def get_db_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    async for session in resources.database.session():
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def client_ip(request: Request) -> str:
    """Client address as resolved by uvicorn's proxy-header handling (trusted proxies only)."""
    return request.client.host if request.client else "unknown"


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        raise AuthenticationError()
    return token.strip()


async def load_context(
    session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> TenantContext:
    now = datetime.now(tz=UTC)
    row = (
        await session.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(User.id == user_id, User.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise InvalidTokenError()
    user, tenant = row
    if not user.is_active or tenant.status != "active":
        raise SessionRevokedError()

    session_alive = await session.scalar(
        select(
            exists().where(
                RefreshToken.family_id == session_id,
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
    )
    if not session_alive:
        raise SessionRevokedError()

    role_names = (
        await session.scalars(
            select(RoleModel.name)
            .join(UserRole, UserRole.role_id == RoleModel.id)
            .where(UserRole.user_id == user_id, UserRole.tenant_id == tenant_id)
        )
    ).all()
    known = {r.value for r in Role}
    roles = frozenset(Role(name) for name in role_names if name in known)
    return TenantContext.for_roles(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
        tenant_slug=tenant.slug,
        session_id=session_id,
    )


async def load_user_context(
    session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> TenantContext:
    """Principal for background work started by a user (exports).

    Re-checks that the user and tenant are still active and reloads roles, so a job
    never runs with permissions the user no longer has; there is no HTTP session to
    check, so the session state is not required.
    """
    row = (
        await session.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(User.id == user_id, User.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise InvalidTokenError()
    user, tenant = row
    if not user.is_active or tenant.status != "active":
        raise SessionRevokedError()
    role_names = (
        await session.scalars(
            select(RoleModel.name)
            .join(UserRole, UserRole.role_id == RoleModel.id)
            .where(UserRole.user_id == user_id, UserRole.tenant_id == tenant_id)
        )
    ).all()
    known = {r.value for r in Role}
    return TenantContext.for_roles(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=frozenset(Role(name) for name in role_names if name in known),
        tenant_slug=tenant.slug,
    )


async def get_principal(
    request: Request, resources: ResourcesDep, session: DbSessionDep
) -> AsyncIterator[TenantContext]:
    claims = resources.tokens.decode_access_token(_bearer_token(request))
    ctx = await load_context(
        session, user_id=claims.user_id, tenant_id=claims.tenant_id, session_id=claims.session_id
    )
    with bind_context(ctx):
        yield ctx


PrincipalDep = Annotated[TenantContext, Depends(get_principal)]


def require(*permissions: Permission) -> Callable[[TenantContext], TenantContext]:
    """Dependency factory: ``ctx: Annotated[TenantContext, Depends(require(P.X))]``."""

    def dependency(ctx: PrincipalDep) -> TenantContext:
        ctx.require(*permissions)
        return ctx

    return dependency
