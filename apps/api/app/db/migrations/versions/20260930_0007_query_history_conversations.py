"""query history grouped into conversations

``conversation_id`` groups the questions of one chat (the side panel's conversation);
follow-ups inherit their parent's. Existing questions become one-question
conversations (``conversation_id = id``).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("query_history", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE query_history SET conversation_id = id WHERE conversation_id IS NULL")
    op.create_index(
        "ix_query_history_tenant_user_conversation",
        "query_history",
        ["tenant_id", "user_id", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_query_history_tenant_user_conversation", table_name="query_history")
    op.drop_column("query_history", "conversation_id")
