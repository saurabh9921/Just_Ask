"""Query history and user feedback."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin

QUERY_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "planned",
    "clarification",
    "refused",
    "failed",
    "cancelled",
)
_QUERY_STATUSES_SQL = ", ".join(f"'{s}'" for s in QUERY_STATUSES)


class QueryRecord(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One analytics question and the governed query executed for it.

    Result rows are never stored here. ``query_plan`` is the validated,
    database-independent plan; ``compiled_sql_hash`` identifies the compiled SQL
    without persisting literal parameter values.
    """

    __tablename__ = "query_history"
    __table_args__ = (
        CheckConstraint(f"status IN ({_QUERY_STATUSES_SQL})", name="status_valid"),
        Index("ix_query_history_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index(
            "ix_query_history_tenant_user_conversation", "tenant_id", "user_id", "conversation_id"
        ),
        Index("ix_query_history_plan_hash", "tenant_id", "normalized_plan_hash"),
        Index(
            "uq_query_history_idempotency",
            "tenant_id",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("db_connections.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    # The chat this question belongs to (follow-ups inherit their parent's); rows from
    # before conversations existed use their own id.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    intent: Mapped[str | None] = mapped_column(String(64))
    query_plan: Mapped[dict[str, Any] | None] = mapped_column()
    normalized_plan_hash: Mapped[str | None] = mapped_column(String(64))
    compiled_sql_hash: Mapped[str | None] = mapped_column(String(64))
    semantic_version: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    saved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()
    deleted_at: Mapped[datetime | None] = mapped_column()


class Feedback(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="rating_valid"),
        # One rating per user and query; re-rating updates it.
        Index("uq_feedback_user_query", "tenant_id", "user_id", "query_id", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("query_history.id", ondelete="CASCADE"), index=True, nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    categories: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column()
