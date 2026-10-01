"""connections: live-name uniqueness, status check, last test error; metadata snapshots

* ``db_connections``: the (tenant_id, name) unique constraint becomes a partial
  unique index over live rows (``deleted_at IS NULL``) so soft-deleted connections
  do not block re-use of a name; adds ``last_test_error_code`` and a status check.
* ``metadata_snapshots`` / ``metadata_objects``: versioned structure-only metadata.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23 06:25:55.882407+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_snapshots",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("server_version", sa.String(length=255), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=True),
        sa.Column("added_count", sa.Integer(), nullable=True),
        sa.Column("changed_count", sa.Integer(), nullable=True),
        sa.Column("removed_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name=op.f("ck_metadata_snapshots_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["db_connections.id"],
            name=op.f("fk_metadata_snapshots_connection_id_db_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_metadata_snapshots_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_metadata_snapshots_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_snapshots")),
    )
    with op.batch_alter_table("metadata_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_metadata_snapshots_connection_version", ["connection_id", "version"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_metadata_snapshots_tenant_id"), ["tenant_id"], unique=False
        )

    op.create_table(
        "metadata_objects",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("row_estimate", sa.BigInteger(), nullable=True),
        sa.Column(
            "definition",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('table', 'view', 'materialized_view')",
            name=op.f("ck_metadata_objects_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["db_connections.id"],
            name=op.f("fk_metadata_objects_connection_id_db_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["metadata_snapshots.id"],
            name=op.f("fk_metadata_objects_snapshot_id_metadata_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_metadata_objects_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_objects")),
    )
    with op.batch_alter_table("metadata_objects", schema=None) as batch_op:
        batch_op.create_index(
            "ix_metadata_objects_connection_name", ["connection_id", "name"], unique=False
        )
        batch_op.create_index(
            "ix_metadata_objects_snapshot_object",
            ["snapshot_id", "schema_name", "name"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_metadata_objects_tenant_id"), ["tenant_id"], unique=False
        )

    with op.batch_alter_table("db_connections", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_test_error_code", sa.String(length=64), nullable=True))
        batch_op.drop_constraint(batch_op.f("uq_db_connections_tenant_id_name"), type_="unique")
        batch_op.create_index(
            "uq_db_connections_live_name",
            ["tenant_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )
        batch_op.create_check_constraint(
            op.f("ck_db_connections_status_valid"), "status IN ('untested', 'ok', 'error')"
        )


def downgrade() -> None:
    with op.batch_alter_table("db_connections", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_db_connections_status_valid"), type_="check")
        batch_op.drop_index(
            "uq_db_connections_live_name",
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )
        batch_op.create_unique_constraint(
            batch_op.f("uq_db_connections_tenant_id_name"), ["tenant_id", "name"]
        )
        batch_op.drop_column("last_test_error_code")

    with op.batch_alter_table("metadata_objects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metadata_objects_tenant_id"))
        batch_op.drop_index("ix_metadata_objects_snapshot_object")
        batch_op.drop_index("ix_metadata_objects_connection_name")

    op.drop_table("metadata_objects")
    with op.batch_alter_table("metadata_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metadata_snapshots_tenant_id"))
        batch_op.drop_index("ix_metadata_snapshots_connection_version")

    op.drop_table("metadata_snapshots")
