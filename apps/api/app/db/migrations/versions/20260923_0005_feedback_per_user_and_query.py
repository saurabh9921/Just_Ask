"""feedback: one rating per user and query (re-rating updates it)

Duplicates (none are expected before Phase 5) are collapsed to the newest row
before the unique index is created.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23 08:16:05.083306+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM feedback WHERE EXISTS ("
            "SELECT 1 FROM feedback AS newer "
            "WHERE newer.tenant_id = feedback.tenant_id "
            "AND newer.user_id = feedback.user_id "
            "AND newer.query_id = feedback.query_id "
            "AND (newer.created_at > feedback.created_at "
            "OR (newer.created_at = feedback.created_at AND newer.id > feedback.id)))"
        )
    )
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            "uq_feedback_user_query", ["tenant_id", "user_id", "query_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_index("uq_feedback_user_query")
        batch_op.drop_column("updated_at")
