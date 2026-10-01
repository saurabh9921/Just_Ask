"""semantic versions: versioned semantic-layer ingestion per connection

One ``active`` version per connection (partial unique index); retrieval filters
vector-store points by it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23 06:59:23.722062+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_versions",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("metadata_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "document_counts",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "entity_bindings",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'building', 'active', 'superseded', 'failed')",
            name=op.f("ck_semantic_versions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["db_connections.id"],
            name=op.f("fk_semantic_versions_connection_id_db_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metadata_snapshot_id"],
            ["metadata_snapshots.id"],
            name=op.f("fk_semantic_versions_metadata_snapshot_id_metadata_snapshots"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_semantic_versions_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_semantic_versions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_versions")),
    )
    with op.batch_alter_table("semantic_versions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_semantic_versions_connection_version", ["connection_id", "version"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_semantic_versions_tenant_id"), ["tenant_id"], unique=False
        )
        batch_op.create_index(
            "uq_semantic_versions_one_active",
            ["connection_id"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("semantic_versions", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_semantic_versions_one_active",
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
        batch_op.drop_index(batch_op.f("ix_semantic_versions_tenant_id"))
        batch_op.drop_index("ix_semantic_versions_connection_version")

    op.drop_table("semantic_versions")
