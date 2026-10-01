"""The explicit tool allow-list of the analytics agent.

The model never selects or calls tools: the finite state machine calls them.
Every tool must be one of ``ALLOWED_TOOLS`` (there is deliberately no generic
database/SQL tool) and must return an instance of its declared Pydantic output
model - the registry checks both on every call.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.connectors.common.types import QueryResult
from app.core.logging import get_logger
from app.query.compiler import CompiledQuery
from app.query.validator import ResolvedPlan

logger = get_logger(__name__)

ALLOWED_TOOLS: Final = frozenset(
    {
        "get_current_user_context",
        "list_allowed_connections",
        "retrieve_semantic_entities",
        "retrieve_business_glossary",
        "retrieve_similar_questions",
        "build_query_plan",
        "validate_query_plan",
        "compile_sql",
        "estimate_query_cost",
        "execute_readonly_query",
        "format_result",
        "request_clarification",
    }
)


class ToolNotAllowedError(RuntimeError):
    pass


# ---------------------------------------------------------------- outputs
class UserContextOut(BaseModel):
    tenant_id: str
    tenant_slug: str
    user_id: str
    permissions: list[str]


class AllowedConnectionsOut(BaseModel):
    connection_ids: list[str]


class RetrievedDocOut(BaseModel):
    collection: str
    doc_key: str
    title: str
    text: str
    entity_name: str | None
    score: float
    untrusted_flagged: bool


class RetrievalOut(BaseModel):
    documents: list[RetrievedDocOut]


class PlanValidationOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    valid: bool
    retryable: bool = False
    issues: list[str] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)
    resolved: ResolvedPlan | None = Field(default=None, exclude=True)


class CompiledSqlOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str
    previous_sql: str | None
    compiled: CompiledQuery = Field(exclude=True)


class CostOut(BaseModel):
    source: str
    estimated_cost: float | None
    estimated_rows: float | None


class ExecutionOut(BaseModel):
    current: QueryResult
    previous: QueryResult | None
    execution_ms: float


class ClarificationOut(BaseModel):
    question: str


ToolFn = Callable[..., Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    output: type[BaseModel]
    fn: ToolFn


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.calls: list[tuple[str, float]] = []

    def register(self, name: str, output: type[BaseModel], fn: ToolFn) -> None:
        if name not in ALLOWED_TOOLS:
            raise ToolNotAllowedError(f"tool '{name}' is not on the allow-list")
        self._tools[name] = ToolSpec(name, output, fn)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    async def call(self, name: str, **kwargs: Any) -> BaseModel:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotAllowedError(f"tool '{name}' is not registered")
        started = time.perf_counter()
        result = await spec.fn(**kwargs)
        if not isinstance(result, spec.output):
            raise TypeError(
                f"tool '{name}' returned {type(result).__name__}, expected {spec.output.__name__}"
            )
        self.calls.append((name, round((time.perf_counter() - started) * 1000, 2)))
        return result
