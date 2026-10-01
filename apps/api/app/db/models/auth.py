"""Refresh tokens (rotation with reuse detection)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Only a SHA-256 hash of the refresh token is stored.

    Tokens issued by rotation share a ``family_id``. Presenting an already-rotated
    token revokes the whole family (stolen-token reuse detection).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family_id", "family_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column()
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
