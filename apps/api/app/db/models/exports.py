"""Background exports of query results (Phase 6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin

EXPORT_STATUSES = ("pending", "running", "succeeded", "failed", "expired", "cancelled")
_EXPORT_STATUSES_SQL = ", ".join(f"'{s}'" for s in EXPORT_STATUSES)


class ExportJob(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One export of a governed query's result set to a temporary file.

    The file lives in ``EXPORT_DIR`` under an opaque name and is deleted when the job
    expires (``expires_at``) or is deleted. Rows never enter the database; only
    counts, sizes and the file digest are recorded.
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_EXPORT_STATUSES_SQL})", name="status_valid"),
        CheckConstraint("format IN ('csv', 'json')", name="format_valid"),
        CheckConstraint("scope IN ('result', 'all_rows')", name="scope_valid"),
        Index("ix_export_jobs_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_export_jobs_status_expires", "status", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("query_history.id", ondelete="CASCADE"), index=True, nullable=False
    )
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="result", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    truncated: Mapped[bool | None] = mapped_column()
    file_name: Mapped[str | None] = mapped_column(String(200))
    storage_key: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    downloaded_at: Mapped[datetime | None] = mapped_column()
