"""Static RBAC catalog: roles, permissions and the role -> permission mapping.

This module is the single source of truth. The initial Alembic migration seeds
the same catalog into the database; ``tests/unit/test_permissions.py`` asserts the
two never drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    DATA_ADMIN = "data_admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class Permission(StrEnum):
    CONNECTIONS_READ = "connections.read"
    CONNECTIONS_MANAGE = "connections.manage"
    METADATA_READ = "metadata.read"
    ANALYTICS_QUERY = "analytics.query"
    ANALYTICS_EXPORT = "analytics.export"
    HISTORY_READ = "history.read"
    FEEDBACK_WRITE = "feedback.write"
    ADMIN_MANAGE = "admin.manage"


PERMISSION_DESCRIPTIONS: Final = MappingProxyType(
    {
        Permission.CONNECTIONS_READ: "View database connections available to the user",
        Permission.CONNECTIONS_MANAGE: "Create, update, test and delete database connections",
        Permission.METADATA_READ: "View synchronised entity and schema metadata",
        Permission.ANALYTICS_QUERY: "Ask analytical questions and run governed queries",
        Permission.ANALYTICS_EXPORT: "Export query results (CSV/JSON, async exports)",
        Permission.HISTORY_READ: "View own query history",
        Permission.FEEDBACK_WRITE: "Submit feedback on answers",
        Permission.ADMIN_MANAGE: "Manage users, roles and tenant settings",
    }
)

_ALL = frozenset(Permission)
_ANALYST = frozenset(
    {
        Permission.CONNECTIONS_READ,
        Permission.METADATA_READ,
        Permission.ANALYTICS_QUERY,
        Permission.ANALYTICS_EXPORT,
        Permission.HISTORY_READ,
        Permission.FEEDBACK_WRITE,
    }
)

ROLE_PERMISSIONS: Final[MappingProxyType[Role, frozenset[Permission]]] = MappingProxyType(
    {
        # platform_admin additionally crosses tenant boundaries for platform
        # operations; that is enforced by tenant_context, not by a permission.
        Role.PLATFORM_ADMIN: _ALL,
        Role.TENANT_ADMIN: _ALL,
        Role.DATA_ADMIN: _ANALYST | {Permission.CONNECTIONS_MANAGE},
        Role.ANALYST: _ANALYST,
        Role.VIEWER: _ANALYST - {Permission.ANALYTICS_EXPORT},
    }
)

ROLE_DESCRIPTIONS: Final = MappingProxyType(
    {
        Role.PLATFORM_ADMIN: "Operates the platform across tenants",
        Role.TENANT_ADMIN: "Administers a single tenant",
        Role.DATA_ADMIN: "Manages connections and the semantic layer for a tenant",
        Role.ANALYST: "Queries and exports governed analytics",
        Role.VIEWER: "Queries governed analytics without export",
    }
)


def permissions_for_roles(roles: Iterable[Role | str]) -> frozenset[Permission]:
    """Union of permissions granted by ``roles``. Unknown role names grant nothing."""
    granted: set[Permission] = set()
    for role in roles:
        try:
            granted |= ROLE_PERMISSIONS[Role(role)]
        except ValueError:
            continue
    return frozenset(granted)
