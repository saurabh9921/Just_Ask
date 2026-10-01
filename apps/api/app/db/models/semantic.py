"""Semantic-layer ingestion versions per (tenant, connection).

Retrieval always filters on the connection's single ``active`` version, so a new
version can be built in the vector store without affecting readers and then
switched atomically. A partial unique index guarantees one active version.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin


class SemanticVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "semantic_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'building', 'active', 'superseded', 'failed')",
            name="status_valid",
        ),
        Index("ix_semantic_versions_connection_version", "connection_id", "version", unique=True),
        Index(
            "uq_semantic_versions_one_active",
            "connection_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("metadata_snapshots.id", ondelete="SET NULL")
    )
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    document_counts: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    entity_bindings: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    activated_at: Mapped[datetime | None] = mapped_column()
