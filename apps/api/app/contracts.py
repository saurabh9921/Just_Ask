"""Shared API contracts rendered as JSON Schema (``packages/contracts/``).

The Pydantic models stay the single source of truth; clients (extension, evaluation
harness) consume the exported schemas. ``tests/unit/test_contracts.py`` fails when
the committed files drift from the models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from app.agents.state import ProgressEvent
from app.query.plans import QueryPlan
from app.schemas.analytics import (
    CancelResponse,
    FollowUpRequest,
    QueryRecordOut,
    QueryRequest,
    QueryResponse,
    StreamRequest,
)
from app.schemas.exports import DownloadTokenOut, ExportCreate, ExportOut, ExportPage
from app.schemas.feedback import FeedbackCreate, FeedbackOut, FeedbackPage
from app.schemas.history import (
    ConversationDetail,
    ConversationPage,
    HistoryDetail,
    HistoryPage,
    HistoryUpdate,
)

CONTRACTS: dict[str, type[BaseModel]] = {
    "query-plan.schema.json": QueryPlan,
    "analytics-query-request.schema.json": QueryRequest,
    "analytics-follow-up-request.schema.json": FollowUpRequest,
    "analytics-stream-request.schema.json": StreamRequest,
    "analytics-query-response.schema.json": QueryResponse,
    "analytics-query-record.schema.json": QueryRecordOut,
    "analytics-cancel-response.schema.json": CancelResponse,
    "analytics-progress-event.schema.json": ProgressEvent,
    "history-page.schema.json": HistoryPage,
    "history-detail.schema.json": HistoryDetail,
    "history-update-request.schema.json": HistoryUpdate,
    "history-conversation-page.schema.json": ConversationPage,
    "history-conversation-detail.schema.json": ConversationDetail,
    "feedback-create-request.schema.json": FeedbackCreate,
    "feedback.schema.json": FeedbackOut,
    "feedback-page.schema.json": FeedbackPage,
    "export-create-request.schema.json": ExportCreate,
    "export.schema.json": ExportOut,
    "export-page.schema.json": ExportPage,
    "export-download-token.schema.json": DownloadTokenOut,
}


def render_contracts() -> dict[str, dict[str, Any]]:
    rendered: dict[str, dict[str, Any]] = {}
    for name, model in CONTRACTS.items():
        mode: Literal["validation", "serialization"] = (
            "validation"
            if name.endswith("request.schema.json") or model is QueryPlan
            else "serialization"
        )
        schema = model.model_json_schema(mode=mode)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://erp-insight-ai.local/contracts/{name}"
        rendered[name] = schema
    return rendered
