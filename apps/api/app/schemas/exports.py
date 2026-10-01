"""Background export models (``/api/v1/exports``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import StrictModel


class ExportCreate(StrictModel):
    query_id: uuid.UUID
    format: Literal["csv", "json"] = "csv"
    # "result": the same rows as the answer; "all_rows": the same query without the
    # interactive row cap (up to EXPORT_MAX_ROWS) - detail data, so confirmation is
    # mandatory.
    scope: Literal["result", "all_rows"] = "result"
    confirm: bool = False


class ExportOut(BaseModel):
    export_id: uuid.UUID
    query_id: uuid.UUID
    format: str
    scope: str
    status: str
    row_count: int | None
    byte_size: int | None
    truncated: bool | None
    file_name: str | None
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    downloaded_at: datetime | None


class ExportPage(BaseModel):
    items: list[ExportOut]


class DownloadTokenOut(BaseModel):
    """One-time, short-lived download URL (bearer-less, like a presigned URL)."""

    url: str
    expires_in: int
