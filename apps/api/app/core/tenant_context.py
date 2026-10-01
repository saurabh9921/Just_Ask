"""Request-scoped tenant and principal context.

The authenticated principal is resolved once per request (Phase 2: from the JWT)
and stored in a context variable. Services read it via :func:`require_context`
instead of accepting tenant IDs from request bodies, which prevents a client from
addressing another tenant's data by changing a parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.logging import tenant_id_var, user_id_var
from app.core.permissions import Permission, Role, permissions_for_roles


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    roles: frozenset[Role]
    permissions: frozenset[Permission] = field(default=frozenset())
    tenant_slug: str = ""
    session_id: uuid.UUID | None = None

    @classmethod
    def for_roles(
        cls,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        roles: frozenset[Role],
        tenant_slug: str = "",
        session_id: uuid.UUID | None = None,
    ) -> TenantContext:
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            permissions=permissions_for_roles(roles),
            tenant_slug=tenant_slug,
            session_id=session_id,
        )

    @property
    def is_platform_admin(self) -> bool:
        return Role.PLATFORM_ADMIN in self.roles

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, *permissions: Permission) -> None:
        missing = [p for p in permissions if p not in self.permissions]
        if missing:
            raise PermissionDeniedError(details={"missing_permissions": sorted(missing)})

    def assert_same_tenant(self, tenant_id: uuid.UUID) -> None:
        """Deny access to resources owned by another tenant.

        Returns a 404-style denial upstream is preferred to avoid confirming the
        resource exists; callers decide, this only guarantees denial.
        """
        if tenant_id != self.tenant_id:
            raise PermissionDeniedError()


_current: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)


def current_context() -> TenantContext | None:
    return _current.get()


def require_context() -> TenantContext:
    ctx = _current.get()
    if ctx is None:
        raise AuthenticationError()
    return ctx


@contextmanager
def bind_context(ctx: TenantContext) -> Iterator[TenantContext]:
    """Bind ``ctx`` (and its log fields) for the duration of the block."""
    token = _current.set(ctx)
    tenant_token = tenant_id_var.set(str(ctx.tenant_id))
    user_token = user_id_var.set(str(ctx.user_id))
    try:
        yield ctx
    finally:
        user_id_var.reset(user_token)
        tenant_id_var.reset(tenant_token)
        _current.reset(token)
