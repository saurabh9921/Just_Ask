"""read replicas on connections; background export jobs

Analytical statements can go to an optional read replica (metadata stays on the
primary); ``export_jobs`` tracks asynchronous CSV/JSON exports (files live in
``EXPORT_DIR``, never in the database).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23 09:23:37.526705+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("query_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("file_name", sa.String(length=200), nullable=True),
        sa.Column("storage_key", sa.String(length=100), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("format IN ('csv', 'json')", name=op.f("ck_export_jobs_format_valid")),
        sa.CheckConstraint(
            "scope IN ('result', 'all_rows')", name=op.f("ck_export_jobs_scope_valid")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'expired', 'cancelled')",
            name=op.f("ck_export_jobs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["query_history.id"],
            name=op.f("fk_export_jobs_query_id_query_history"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_export_jobs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_export_jobs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_jobs")),
    )
    with op.batch_alter_table("export_jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_export_jobs_query_id"), ["query_id"], unique=False)
        batch_op.create_index(
            "ix_export_jobs_status_expires", ["status", "expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_export_jobs_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_export_jobs_tenant_user_created",
            ["tenant_id", "user_id", "created_at"],
            unique=False,
        )

    with op.batch_alter_table("db_connections", schema=None) as batch_op:
        batch_op.add_column(sa.Column("read_replica_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("read_replica_port", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_db_connections_replica_port_range"),
            "read_replica_port IS NULL OR (read_replica_port > 0 AND read_replica_port < 65536)",
        )


def downgrade() -> None:
    with op.batch_alter_table("db_connections", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_db_connections_replica_port_range"), type_="check")
        batch_op.drop_column("read_replica_port")
        batch_op.drop_column("read_replica_host")

    with op.batch_alter_table("export_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_export_jobs_tenant_user_created")
        batch_op.drop_index(batch_op.f("ix_export_jobs_tenant_id"))
        batch_op.drop_index("ix_export_jobs_status_expires")
        batch_op.drop_index(batch_op.f("ix_export_jobs_query_id"))

    op.drop_table("export_jobs")
