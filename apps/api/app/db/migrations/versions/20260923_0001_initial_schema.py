"""initial schema: tenancy, RBAC, connections, history, feedback, audit

Also seeds the static RBAC catalog (roles, permissions, role_permissions). The
catalog is copied here rather than imported from ``app.core.permissions`` so the
migration stays reproducible if application code changes later;
``tests/unit/test_migrations.py`` asserts both remain identical.

On PostgreSQL, ``audit_events`` is made append-only with a trigger.

Revision ID: 0001
Revises:
Create Date: 2026-09-23 05:56:19.760534+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("name", name=op.f("uq_permissions_name")),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
    )
    op.create_table(
        "tenants",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "settings",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name=op.f("ck_tenants_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "encrypted_secrets",
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_encrypted_secrets_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_encrypted_secrets")),
    )
    op.create_index(
        op.f("ix_encrypted_secrets_tenant_id"), "encrypted_secrets", ["tenant_id"], unique=False
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("external_subject", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_users_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tenant_id", "email", name=op.f("uq_users_tenant_id_email")),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'error')", name=op.f("ck_audit_events_outcome_valid")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_audit_events_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_tenant_occurred",
        "audit_events",
        ["tenant_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_type_occurred", "audit_events", ["event_type", "occurred_at"], unique=False
    )

    op.create_table(
        "db_connections",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column("schema_name", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("secret_ref", sa.String(length=512), nullable=True),
        sa.Column("tls_mode", sa.String(length=16), nullable=False),
        sa.Column("tls_ca_ref", sa.String(length=512), nullable=True),
        sa.Column("pool_min_size", sa.Integer(), nullable=False),
        sa.Column("pool_max_size", sa.Integer(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("query_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_rows", sa.Integer(), nullable=False),
        sa.Column(
            "options",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "connector_type IN ('postgresql', 'mysql', 'sqlserver', 'oracle', 'sap_hana', 'snowflake', 'bigquery', 'sqlite')",
            name=op.f("ck_db_connections_connector_type_valid"),
        ),
        sa.CheckConstraint(
            "tls_mode IN ('disable', 'prefer', 'require', 'verify-ca', 'verify-full')",
            name=op.f("ck_db_connections_tls_mode_valid"),
        ),
        sa.CheckConstraint(
            "max_rows > 0 AND max_rows <= 100000", name=op.f("ck_db_connections_max_rows_range")
        ),
        sa.CheckConstraint(
            "pool_min_size >= 0 AND pool_max_size >= pool_min_size",
            name=op.f("ck_db_connections_pool_range"),
        ),
        sa.CheckConstraint(
            "port IS NULL OR (port > 0 AND port < 65536)", name=op.f("ck_db_connections_port_range")
        ),
        sa.CheckConstraint(
            "query_timeout_seconds > 0 AND query_timeout_seconds <= 600",
            name=op.f("ck_db_connections_timeout_range"),
        ),
        sa.CheckConstraint("read_only = true", name=op.f("ck_db_connections_read_only_enforced")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_db_connections_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_db_connections_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_db_connections")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_db_connections_tenant_id_name")),
    )
    op.create_index(
        op.f("ix_db_connections_tenant_id"), "db_connections", ["tenant_id"], unique=False
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_refresh_tokens_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"], unique=False)
    op.create_index(
        op.f("ix_refresh_tokens_tenant_id"), "refresh_tokens", ["tenant_id"], unique=False
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_roles_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index(op.f("ix_user_roles_tenant_id"), "user_roles", ["tenant_id"], unique=False)

    op.create_table(
        "connection_grants",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["db_connections.id"],
            name=op.f("fk_connection_grants_connection_id_db_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_connection_grants_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_connection_grants_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("connection_id", "role_id", name=op.f("pk_connection_grants")),
    )
    op.create_index(
        op.f("ix_connection_grants_tenant_id"), "connection_grants", ["tenant_id"], unique=False
    )

    op.create_table(
        "query_history",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column(
            "query_plan",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("normalized_plan_hash", sa.String(length=64), nullable=True),
        sa.Column("compiled_sql_hash", sa.String(length=64), nullable=True),
        sa.Column("semantic_version", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("execution_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("saved", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'clarification')",
            name=op.f("ck_query_history_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["db_connections.id"],
            name=op.f("fk_query_history_connection_id_db_connections"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_query_history_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_query_history_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_history")),
    )
    op.create_index(
        "ix_query_history_plan_hash",
        "query_history",
        ["tenant_id", "normalized_plan_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_query_history_tenant_id"), "query_history", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_query_history_tenant_user_created",
        "query_history",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "feedback",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("query_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column(
            "categories",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("comment", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_feedback_rating_valid")),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["query_history.id"],
            name=op.f("fk_feedback_query_id_query_history"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_feedback_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_feedback_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_query_id"), "feedback", ["query_id"], unique=False)
    op.create_index(op.f("ix_feedback_tenant_id"), "feedback", ["tenant_id"], unique=False)

    _seed_rbac()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_AUDIT_APPEND_ONLY_FN)
        op.execute(_AUDIT_APPEND_ONLY_TRIGGER)


PERMISSIONS: tuple[tuple[int, str, str], ...] = (
    (1, "connections.read", "View database connections available to the user"),
    (2, "connections.manage", "Create, update, test and delete database connections"),
    (3, "metadata.read", "View synchronised entity and schema metadata"),
    (4, "analytics.query", "Ask analytical questions and run governed queries"),
    (5, "analytics.export", "Export query results (CSV/JSON, async exports)"),
    (6, "history.read", "View own query history"),
    (7, "feedback.write", "Submit feedback on answers"),
    (8, "admin.manage", "Manage users, roles and tenant settings"),
)

ROLES: tuple[tuple[int, str, str], ...] = (
    (1, "platform_admin", "Operates the platform across tenants"),
    (2, "tenant_admin", "Administers a single tenant"),
    (3, "data_admin", "Manages connections and the semantic layer for a tenant"),
    (4, "analyst", "Queries and exports governed analytics"),
    (5, "viewer", "Queries governed analytics without export"),
)

_ALL = tuple(name for _, name, _ in PERMISSIONS)
_ANALYST = (
    "connections.read",
    "metadata.read",
    "analytics.query",
    "analytics.export",
    "history.read",
    "feedback.write",
)
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "platform_admin": _ALL,
    "tenant_admin": _ALL,
    "data_admin": (*_ANALYST, "connections.manage"),
    "analyst": _ANALYST,
    "viewer": tuple(p for p in _ANALYST if p != "analytics.export"),
}

_AUDIT_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""

_AUDIT_APPEND_ONLY_TRIGGER = """
CREATE TRIGGER trg_audit_events_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()
"""


def _seed_rbac() -> None:
    permissions = sa.table(
        "permissions",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    role_permissions = sa.table(
        "role_permissions", sa.column("role_id", sa.Integer), sa.column("permission_id", sa.Integer)
    )
    op.bulk_insert(permissions, [{"id": i, "name": n, "description": d} for i, n, d in PERMISSIONS])
    op.bulk_insert(roles, [{"id": i, "name": n, "description": d} for i, n, d in ROLES])
    perm_ids = {name: pid for pid, name, _ in PERMISSIONS}
    role_ids = {name: rid for rid, name, _ in ROLES}
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_ids[role], "permission_id": perm_ids[perm]}
            for role, perms in ROLE_PERMISSIONS.items()
            for perm in perms
        ],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_index(op.f("ix_feedback_tenant_id"), table_name="feedback")
    op.drop_index(op.f("ix_feedback_query_id"), table_name="feedback")

    op.drop_table("feedback")
    op.drop_index("ix_query_history_tenant_user_created", table_name="query_history")
    op.drop_index(op.f("ix_query_history_tenant_id"), table_name="query_history")
    op.drop_index("ix_query_history_plan_hash", table_name="query_history")

    op.drop_table("query_history")
    op.drop_index(op.f("ix_connection_grants_tenant_id"), table_name="connection_grants")

    op.drop_table("connection_grants")
    op.drop_index(op.f("ix_user_roles_tenant_id"), table_name="user_roles")

    op.drop_table("user_roles")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_tenant_id"), table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")

    op.drop_table("refresh_tokens")
    op.drop_index(op.f("ix_db_connections_tenant_id"), table_name="db_connections")

    op.drop_table("db_connections")
    op.drop_index("ix_audit_events_type_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_occurred", table_name="audit_events")

    op.drop_table("audit_events")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")

    op.drop_table("users")
    op.drop_table("role_permissions")
    op.drop_index(op.f("ix_encrypted_secrets_tenant_id"), table_name="encrypted_secrets")

    op.drop_table("encrypted_secrets")
    op.drop_table("tenants")
    op.drop_table("roles")
    op.drop_table("permissions")
