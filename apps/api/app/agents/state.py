"""Agent states, progress events and the per-request state record."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentStep(StrEnum):
    RECEIVE_QUESTION = "RECEIVE_QUESTION"
    CLASSIFY_INTENT = "CLASSIFY_INTENT"
    RESOLVE_DATA_SOURCE = "RESOLVE_DATA_SOURCE"
    DETECT_ANALYSIS_INTENT = "DETECT_ANALYSIS_INTENT"
    RESOLVE_ENTITIES = "RESOLVE_ENTITIES"
    RUN_ANALYSIS = "RUN_ANALYSIS"
    RETRIEVE_SEMANTIC_CONTEXT = "RETRIEVE_SEMANTIC_CONTEXT"
    RETRIEVE_SIMILAR_EXAMPLES = "RETRIEVE_SIMILAR_EXAMPLES"
    BUILD_QUERY_PLAN = "BUILD_QUERY_PLAN"
    SELECT_FIELDS = "SELECT_FIELDS"
    VALIDATE_QUERY_PLAN = "VALIDATE_QUERY_PLAN"
    REQUEST_CLARIFICATION_IF_NEEDED = "REQUEST_CLARIFICATION_IF_NEEDED"
    COMPILE_DIALECT_SQL = "COMPILE_DIALECT_SQL"
    VALIDATE_SQL = "VALIDATE_SQL"
    ESTIMATE_COST = "ESTIMATE_COST"
    EXECUTE_READONLY_QUERY = "EXECUTE_READONLY_QUERY"
    FORMAT_RESULT = "FORMAT_RESULT"
    VALIDATE_RESULT = "VALIDATE_RESULT"
    GENERATE_EXPLANATION = "GENERATE_EXPLANATION"
    STORE_AUDIT_EVENT = "STORE_AUDIT_EVENT"
    COMPLETE = "COMPLETE"


STEP_MESSAGES: dict[AgentStep, str] = {
    AgentStep.RECEIVE_QUESTION: "Reading your question",
    AgentStep.CLASSIFY_INTENT: "Understanding what you are asking",
    AgentStep.RESOLVE_DATA_SOURCE: "Checking the data source and your access",
    AgentStep.DETECT_ANALYSIS_INTENT: "Working out what kind of answer you need",
    AgentStep.RESOLVE_ENTITIES: "Finding the company or record you named",
    AgentStep.RUN_ANALYSIS: "Running the analysis queries",
    AgentStep.RETRIEVE_SEMANTIC_CONTEXT: "Finding relevant business definitions",
    AgentStep.RETRIEVE_SIMILAR_EXAMPLES: "Looking at similar questions",
    AgentStep.BUILD_QUERY_PLAN: "Planning the query",
    AgentStep.SELECT_FIELDS: "Choosing fields that match your question",
    AgentStep.VALIDATE_QUERY_PLAN: "Validating the plan against data policy",
    AgentStep.REQUEST_CLARIFICATION_IF_NEEDED: "Checking whether clarification is needed",
    AgentStep.COMPILE_DIALECT_SQL: "Generating database query",
    AgentStep.VALIDATE_SQL: "Checking query safety",
    AgentStep.ESTIMATE_COST: "Estimating query cost",
    AgentStep.EXECUTE_READONLY_QUERY: "Running read-only query",
    AgentStep.FORMAT_RESULT: "Preparing results",
    AgentStep.VALIDATE_RESULT: "Checking the results before answering",
    AgentStep.GENERATE_EXPLANATION: "Summarising the results",
    AgentStep.STORE_AUDIT_EVENT: "Recording audit trail",
    AgentStep.COMPLETE: "Done",
}

OutcomeStatus = Literal["succeeded", "planned", "clarification", "refused", "failed", "cancelled"]


class ProgressEvent(BaseModel):
    query_id: uuid.UUID
    step: AgentStep
    status: Literal["started", "completed", "failed", "skipped"]
    message: str
    elapsed_ms: float
    at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class StepRecord(BaseModel):
    step: AgentStep
    status: Literal["completed", "failed", "skipped"]
    duration_ms: float
    detail: str | None = None


@dataclass
class AgentError:
    code: str
    message: str
    status_code: int
    details: dict[str, Any] = field(default_factory=dict)
