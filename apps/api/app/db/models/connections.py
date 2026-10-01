"""ERP database connections, per-role grants and the encrypted secret store.

Connection rows never contain passwords. ``secret_ref`` points to a secret held
either in an external secret manager (``vault://``, ``aws-sm://``, ``env://``) or
in ``encrypted_secrets`` (``local://<uuid>``), which stores only ciphertext
encrypted with ``ENCRYPTION_KEY`` (envelope encryption, Phase 2).
"""

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
    LargeBinary,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

CONNECTOR_TYPES = (
    "postgresql",
    "mysql",
    "sqlserver",
    "oracle",
    "sap_hana",
    "snowflake",
    "bigquery",
    "sqlite",
)
_CONNECTOR_TYPES_SQL = ", ".join(f"'{t}'" for t in CONNECTOR_TYPES)


class DatabaseConnection(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "db_connections"
    __table_args__ = (
        # Names are unique among *live* connections; soft-deleted rows keep theirs.
        Index(
            "uq_db_connections_live_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("status IN ('untested', 'ok', 'error')", name="status_valid"),
        CheckConstraint(f"connector_type IN ({_CONNECTOR_TYPES_SQL})", name="connector_type_valid"),
        # The MVP is analytics-only: a connection can never be stored as writable.
        CheckConstraint("read_only = true", name="read_only_enforced"),
        CheckConstraint("port IS NULL OR (port > 0 AND port < 65536)", name="port_range"),
        CheckConstraint(
            "read_replica_port IS NULL OR (read_replica_port > 0 AND read_replica_port < 65536)",
            name="replica_port_range",
        ),
        CheckConstraint(
            "query_timeout_seconds > 0 AND query_timeout_seconds <= 600", name="timeout_range"
        ),
        CheckConstraint("max_rows > 0 AND max_rows <= 100000", name="max_rows_range"),
        CheckConstraint("pool_min_size >= 0 AND pool_max_size >= pool_min_size", name="pool_range"),
        CheckConstraint(
            "tls_mode IN ('disable', 'prefer', 'require', 'verify-ca', 'verify-full')",
            name="tls_mode_valid",
        ),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    # Optional read replica for analytical statements (metadata uses the primary).
    read_replica_host: Mapped[str | None] = mapped_column(String(255))
    read_replica_port: Mapped[int | None] = mapped_column(Integer)
    database_name: Mapped[str | None] = mapped_column(String(255))
    schema_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    secret_ref: Mapped[str | None] = mapped_column(String(512))
    tls_mode: Mapped[str] = mapped_column(String(16), default="verify-full", nullable=False)
    tls_ca_ref: Mapped[str | None] = mapped_column(String(512))
    pool_min_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pool_max_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    query_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_rows: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="untested", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column()
    last_test_error_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column()


class ConnectionGrant(Base):
    """Which roles in a tenant may use a connection (connection-level authorization)."""

    __tablename__ = "connection_grants"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("db_connections.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True, nullable=False
    )


class EncryptedSecret(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "encrypted_secrets"

    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
