"""query history: planned/refused statuses and per-user idempotency keys

Phase 4 records explain-only runs (``planned``) and policy refusals (``refused``);
``Idempotency-Key`` replays are looked up by (tenant, user, key).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23 07:37:32.138821+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'clarification')"
_NEW = (
    "status IN ('pending', 'running', 'succeeded', 'planned', 'clarification', 'refused', "
    "'failed', 'cancelled')"
)


def upgrade() -> None:
    with op.batch_alter_table("query_history", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_query_history_status_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_query_history_status_valid"), _NEW)
        batch_op.create_index(
            "uq_query_history_idempotency",
            ["tenant_id", "user_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE query_history SET status = 'failed' WHERE status IN ('planned', 'refused')")
    )
    with op.batch_alter_table("query_history", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_query_history_idempotency",
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )
        batch_op.drop_constraint(op.f("ck_query_history_status_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_query_history_status_valid"), _OLD)
