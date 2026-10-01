"""Metadata discovery models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.connectors.common.types import ColumnInfo, ForeignKeyInfo
from app.schemas.common import ResponseModel


class SyncStatusOut(ResponseModel):
    snapshot_id: uuid.UUID
    version: int
    status: str
    object_count: int | None
    added_count: int | None
    changed_count: int | None
    removed_count: int | None
    error_code: str | None
    server_version: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ConnectionSyncStatus(ResponseModel):
    connection_id: uuid.UUID
    latest: SyncStatusOut | None
    active_version: int | None = Field(description="Latest successfully synced version")
    synced_at: datetime | None


class EntitySummary(ResponseModel):
    schema_name: str
    name: str
    kind: str
    column_count: int
    row_estimate: int | None
    comment: str | None


class EntityList(ResponseModel):
    items: list[EntitySummary]
    total: int
    version: int | None
    synced_at: datetime | None


class EntityDetail(ResponseModel):
    schema_name: str
    name: str
    kind: str
    row_estimate: int | None
    comment: str | None
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo]
    version: int
    fingerprint: str
