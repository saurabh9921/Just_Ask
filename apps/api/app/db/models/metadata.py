"""Versioned metadata snapshots discovered from ERP databases.

Each sync creates a new ``metadata_snapshots`` row (monotonic ``version`` per
connection). ``metadata_objects`` hold structure only (columns, keys, comments,
row estimates) with a SHA-256 ``fingerprint`` per object, so a sync can report
what was added/changed/removed and later phases can re-embed only changed objects.
No data values are ever stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin

SNAPSHOT_STATUSES = ("pending", "running", "succeeded", "failed")


class MetadataSnapshot(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "metadata_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name="status_valid"
        ),
        Index("ix_metadata_snapshots_connection_version", "connection_id", "version", unique=True),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    task_id: Mapped[str | None] = mapped_column(String(64))
    server_version: Mapped[str | None] = mapped_column(String(255))
    object_count: Mapped[int | None] = mapped_column(Integer)
    added_count: Mapped[int | None] = mapped_column(Integer)
    changed_count: Mapped[int | None] = mapped_column(Integer)
    removed_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


class MetadataObject(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "metadata_objects"
    __table_args__ = (
        CheckConstraint("kind IN ('table', 'view', 'materialized_view')", name="kind_valid"),
        Index(
            "ix_metadata_objects_snapshot_object",
            "snapshot_id",
            "schema_name",
            "name",
            unique=True,
        ),
        Index("ix_metadata_objects_connection_name", "connection_id", "name"),
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    row_estimate: Mapped[int | None] = mapped_column(BigInteger)
    definition: Mapped[dict[str, Any]] = mapped_column(nullable=False)
