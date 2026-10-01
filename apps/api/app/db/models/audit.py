"""Append-only audit log.

On PostgreSQL the initial migration installs a trigger that rejects UPDATE and
DELETE on this table, so audit records cannot be altered through the application
role even by a bug. ``details`` must be passed through ``app.core.logging.redact``
before insert (enforced by the audit service in Phase 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

AUDIT_OUTCOMES = ("success", "denied", "error")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'denied', 'error')", name="outcome_valid"),
        Index("ix_audit_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_events_type_occurred", "event_type", "occurred_at"),
    )

    # BIGINT on PostgreSQL; INTEGER on SQLite so it aliases ROWID (autoincrement).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # Nullable: platform-level events (e.g. failed login for unknown tenant).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
