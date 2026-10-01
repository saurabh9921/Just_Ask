from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.logging import tenant_id_var
from app.core.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    permissions_for_roles,
)
from app.core.tenant_context import (
    TenantContext,
    bind_context,
    current_context,
    require_context,
)


def test_every_role_and_permission_is_described() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert set(ROLE_DESCRIPTIONS) == set(Role)
    assert set(PERMISSION_DESCRIPTIONS) == set(Permission)


def test_viewer_cannot_export_or_manage() -> None:
    viewer = ROLE_PERMISSIONS[Role.VIEWER]
    assert Permission.ANALYTICS_QUERY in viewer
    assert Permission.ANALYTICS_EXPORT not in viewer
    assert Permission.CONNECTIONS_MANAGE not in viewer
    assert Permission.ADMIN_MANAGE not in viewer


def test_only_admin_roles_hold_admin_manage() -> None:
    holders = {r for r, perms in ROLE_PERMISSIONS.items() if Permission.ADMIN_MANAGE in perms}
    assert holders == {Role.PLATFORM_ADMIN, Role.TENANT_ADMIN}


def test_role_hierarchy_is_monotonic() -> None:
    assert ROLE_PERMISSIONS[Role.VIEWER] < ROLE_PERMISSIONS[Role.ANALYST]
    assert ROLE_PERMISSIONS[Role.ANALYST] < ROLE_PERMISSIONS[Role.DATA_ADMIN]
    assert ROLE_PERMISSIONS[Role.DATA_ADMIN] < ROLE_PERMISSIONS[Role.TENANT_ADMIN]


def test_permissions_for_roles_unions_and_ignores_unknown() -> None:
    perms = permissions_for_roles(["viewer", "not-a-role"])
    assert perms == ROLE_PERMISSIONS[Role.VIEWER]
    assert permissions_for_roles([]) == frozenset()


def _ctx(*roles: Role) -> TenantContext:
    return TenantContext.for_roles(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), roles=frozenset(roles)
    )


def test_require_raises_with_missing_permissions() -> None:
    ctx = _ctx(Role.VIEWER)
    ctx.require(Permission.ANALYTICS_QUERY)
    with pytest.raises(PermissionDeniedError) as info:
        ctx.require(Permission.ANALYTICS_QUERY, Permission.ANALYTICS_EXPORT)
    assert info.value.details == {"missing_permissions": ["analytics.export"]}


def test_assert_same_tenant_denies_cross_tenant() -> None:
    ctx = _ctx(Role.TENANT_ADMIN)
    ctx.assert_same_tenant(ctx.tenant_id)
    with pytest.raises(PermissionDeniedError):
        ctx.assert_same_tenant(uuid.uuid4())


def test_bind_context_scopes_context_and_log_fields() -> None:
    assert current_context() is None
    with pytest.raises(AuthenticationError):
        require_context()
    ctx = _ctx(Role.ANALYST)
    with bind_context(ctx):
        assert require_context() is ctx
        assert tenant_id_var.get() == str(ctx.tenant_id)
    assert current_context() is None
    assert tenant_id_var.get() is None
