"""Query history models (``/api/v1/history``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.analytics import QueryResponse
from app.schemas.common import StrictModel


class HistoryItem(BaseModel):
    query_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    question: str
    status: str
    intent: str | None
    connection_id: uuid.UUID | None
    connection_name: str | None
    row_count: int | None
    total_ms: int | None
    error_code: str | None
    saved: bool
    feedback_rating: int | None
    created_at: datetime
    completed_at: datetime | None


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    total: int
    limit: int
    offset: int


class HistoryDetail(HistoryItem):
    semantic_version: int | None
    execution_ms: int | None
    query_plan: dict[str, Any] | None
    response: QueryResponse | None = None
    response_expired: bool = False


class HistoryUpdate(StrictModel):
    saved: bool


class ConversationItem(BaseModel):
    """One chat in the history list: its first question stands for the whole chat."""

    conversation_id: uuid.UUID
    title: str
    question_count: int
    last_question: str
    last_status: str
    connection_id: uuid.UUID | None
    connection_name: str | None
    saved: bool  # any of its questions is saved
    started_at: datetime
    last_activity_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationItem]
    total: int
    limit: int
    offset: int


class ConversationDetail(ConversationItem):
    """The whole chat: every question with its answer (while cached), oldest first."""

    messages: list[HistoryDetail]
