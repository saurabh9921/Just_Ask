"""Analytics request/response models (``/api/v1/analytics``).

The response carries everything the UI must show next to an answer: data source,
freshness, applied date window and filters, security scope (row-filter *field
names*, never their values), rows returned, timings, validation status, source
entities, the plain-English plan description, assumptions, the compiled SQL
(with bind placeholders - literal values are never rendered into it), KPIs,
chart and explanation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.agents.state import OutcomeStatus, StepRecord
from app.query.result_formatter import FormattedResult
from app.schemas.common import StrictModel
from app.schemas.insights import AnalysisOut

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class QueryRequest(StrictModel):
    question: Question
    connection_id: uuid.UUID
    # The chat the question belongs to (history groups by it); omitted = a new chat.
    conversation_id: uuid.UUID | None = None


class FollowUpRequest(StrictModel):
    question: Question
    parent_query_id: uuid.UUID
    connection_id: uuid.UUID | None = None  # defaults to the parent's connection
    conversation_id: uuid.UUID | None = None  # defaults to the parent's conversation


class StreamRequest(StrictModel):
    """Streaming variant of ``/query`` and ``/follow-up`` (give a parent to follow up)."""

    question: Question
    connection_id: uuid.UUID | None = None
    parent_query_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _source(self) -> StreamRequest:
        if self.connection_id is None and self.parent_query_id is None:
            raise ValueError("connection_id or parent_query_id is required")
        return self


class ConnectionRef(BaseModel):
    id: uuid.UUID
    name: str | None = None
    connector_type: str | None = None


class DateWindowOut(BaseModel):
    field: str
    start: date
    end_exclusive: date
    label: str


class AppliedFilterOut(BaseModel):
    field: str
    op: str
    value: Any


class AppliedFiltersOut(BaseModel):
    date_window: DateWindowOut | None = None
    previous_window: DateWindowOut | None = None
    filters: list[AppliedFilterOut] = Field(default_factory=list)


class SecurityScopeOut(BaseModel):
    tenant: str
    row_filter_fields: list[str] = Field(default_factory=list)
    read_only: bool = True


class ValidationOut(BaseModel):
    plan_valid: bool
    sql_validated: bool
    corrections: int = 0
    confidence: float | None = None


class CostOut(BaseModel):
    source: str
    estimated_cost: float | None = None
    estimated_rows: int | None = None


class ExplanationOut(BaseModel):
    summary: str
    highlights: list[str] = Field(default_factory=list)
    source: Literal["llm", "system"]


class QueryErrorOut(BaseModel):
    code: str
    message: str
    status_code: int = 500
    details: dict[str, Any] = Field(default_factory=dict)


class UsageOut(BaseModel):
    """Model calls and tokens spent on this question (0 tokens for the mock provider)."""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CacheInfoOut(BaseModel):
    """Whether the rows came from the query-result cache (and how old they are)."""

    hit: bool = False
    age_seconds: float | None = None


class TimingsOut(BaseModel):
    total_ms: float | None = None
    execution_ms: float | None = None
    llm_ms: float | None = None


class QueryResponse(BaseModel):
    query_id: uuid.UUID
    status: OutcomeStatus
    question: str
    intent: str | None = None
    connection: ConnectionRef
    semantic_version: int | None = None
    data_freshness: str | None = None
    plan: dict[str, Any] | None = None
    description: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    source_entities: list[str] = Field(default_factory=list)
    applied_filters: AppliedFiltersOut = Field(default_factory=AppliedFiltersOut)
    security_scope: SecurityScopeOut
    validation: ValidationOut
    sql: str | None = None
    previous_sql: str | None = None
    cost: CostOut | None = None
    result: FormattedResult | None = None
    rows_returned: int | None = None
    explanation: ExplanationOut | None = None
    clarification_question: str | None = None
    refusal_reason: str | None = None
    error: QueryErrorOut | None = None
    timings: TimingsOut = Field(default_factory=TimingsOut)
    steps: list[StepRecord] = Field(default_factory=list)
    cache: CacheInfoOut = Field(default_factory=CacheInfoOut)
    usage: UsageOut = Field(default_factory=UsageOut)
    replayed: bool = False
    # Structured, validated analytics (metrics, insights, data quality, chart decision).
    analysis: AnalysisOut | None = None


class QueryRecordOut(BaseModel):
    """``GET /query/{id}``: the stored record plus the cached response while it lives."""

    query_id: uuid.UUID
    status: str
    question: str
    connection_id: uuid.UUID | None
    intent: str | None
    semantic_version: int | None
    row_count: int | None
    execution_ms: int | None
    total_ms: int | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    response: QueryResponse | None = None
    response_expired: bool = False


class CancelResponse(BaseModel):
    query_id: uuid.UUID
    status: str
    cancel_requested: bool
