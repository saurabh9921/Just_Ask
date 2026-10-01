"""Deterministic mock LLM provider (CPU-only development and tests).

Handlers are registered per response model. Their output is serialised to JSON
and passed through the *same* strict parser as real model output, so the mock
exercises the validation path. Built-in handlers implement simple, predictable
rules for Phase 3 schemas; tests can register scripted handlers.

Not allowed in production (settings refuse ``LLM_PROVIDER=mock``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.agents.schemas import EntityResolution, IntentClassification, ResultExplanation
from app.core.metrics import LLM_VALIDATION_FAILURES
from app.llm.base import ChatMessage, LLMOutputError
from app.llm.mock_planner import explain_result, plan_query
from app.llm.structured_output import StructuredOutputError, parse_structured
from app.llm.usage import record_llm_usage
from app.query.plans import QueryPlan
from app.rag.text import tokenize

Handler = Callable[[list[ChatMessage]], str | dict[str, Any] | BaseModel]

_QUESTION = re.compile(r"<<<QUESTION>>>\n(.*?)\n<<<END QUESTION>>>", re.DOTALL)
_VOCAB = re.compile(
    r"^- (\w+): measures=\[(.*?)\] dimensions=\[(.*?)\] time_fields=\[(.*?)\]$", re.MULTILINE
)
_CONTEXT = re.compile(
    r'<<<CONTEXT id="\d+" source="(\w+)" key="([^"]*)"[^>]*>>>\n(.*?)\n<<<END CONTEXT>>>', re.DOTALL
)


def _question(messages: list[ChatMessage]) -> str:
    match = _QUESTION.search(messages[-1]["content"])
    return match.group(1) if match else messages[-1]["content"]


def _names(raw: str) -> list[str]:
    return [n.strip().strip("'\"") for n in raw.split(",") if n.strip()]


def classify_intent(messages: list[ChatMessage]) -> IntentClassification:  # noqa: PLR0911 - rule table
    q = _question(messages).lower()
    ordered = tokenize(q, keep_stopwords=True)
    words = set(ordered)
    destructive = {"delete", "update", "insert", "drop", "truncate", "grant", "alter", "remove"}
    if ordered and ordered[0] in destructive:
        return IntentClassification(
            intent="unsupported", confidence=0.95, reason="data modification"
        )
    if words & {"why", "explain", "reason"}:
        return IntentClassification(intent="explanation", confidence=0.8)
    if words & {"compare", "versus", "vs"} or ("than" in words and "last" in words):
        return IntentClassification(intent="comparison", confidence=0.8)
    if words & {"top", "highest", "lowest", "best", "worst", "rank"}:
        return IntentClassification(intent="ranking", confidence=0.8)
    if words & {"trend", "over", "monthly", "weekly", "movement"}:
        return IntentClassification(intent="trend", confidence=0.7)
    if len(words) <= 2:
        return IntentClassification(
            intent="clarification", confidence=0.6, needs_clarification=True, reason="too short"
        )
    if words & {"list", "which", "show"} and not words & {"total", "sum", "average", "count"}:
        return IntentClassification(intent="detail", confidence=0.6)
    return IntentClassification(intent="aggregation", confidence=0.7)


def resolve_entities(messages: list[ChatMessage]) -> EntityResolution:
    """Score each vocabulary entity by token overlap with question + retrieved context."""
    content = messages[-1]["content"]
    question = set(tokenize(_question(messages)))
    context_entities: dict[str, float] = {}
    for rank, match in enumerate(_CONTEXT.finditer(content), start=1):
        key = match.group(2)
        entity = key.split(":", 1)[-1].split(".", 1)[0]
        context_entities[entity] = context_entities.get(entity, 0.0) + 1.0 / rank
    best: tuple[float, str, list[str], list[str], list[str]] | None = None
    for m in _VOCAB.finditer(content):
        entity, measures, dims, times = (
            m.group(1),
            _names(m.group(2)),
            _names(m.group(3)),
            _names(m.group(4)),
        )
        hit_measures = [x for x in measures if set(tokenize(x)) & question]
        hit_dims = [x for x in dims if set(tokenize(x)) & question]
        score = context_entities.get(entity, 0.0) + len(hit_measures) + len(hit_dims)
        score += 0.5 * len(set(tokenize(entity)) & question)
        if best is None or score > best[0]:
            best = (score, entity, hit_measures, hit_dims, times)
    if best is None or best[0] <= 0:
        return EntityResolution(entity=None, confidence=0.1, unresolved_terms=sorted(question)[:10])
    score, entity, measures, dims, times = best
    return EntityResolution(
        entity=entity,
        measures=measures[:10],
        dimensions=dims[:10],
        time_field=times[0] if times else None,
        confidence=min(0.95, 0.3 + 0.15 * score),
    )


DEFAULT_HANDLERS: dict[type[BaseModel], Handler] = {
    IntentClassification: classify_intent,
    EntityResolution: resolve_entities,
    QueryPlan: plan_query,
    ResultExplanation: explain_result,
}


class MockLLMProvider:
    name = "mock"

    def __init__(self, handlers: dict[type[BaseModel], Handler] | None = None) -> None:
        self.handlers: dict[type[BaseModel], Handler] = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def generate_structured[M: BaseModel](
        self,
        messages: list[ChatMessage],
        response_model: type[M],
        temperature: float = 0,
    ) -> M:
        self.calls.append((response_model.__name__, messages))
        record_llm_usage(0, 0)  # a call, but the mock has no tokenizer
        handler = self.handlers.get(response_model)
        if handler is None:
            raise LLMOutputError(
                details={"reason": "NO_MOCK_HANDLER", "schema": response_model.__name__}
            )
        produced = handler(messages)
        if isinstance(produced, BaseModel):
            raw = produced.model_dump_json()
        elif isinstance(produced, dict):
            raw = json.dumps(produced)
        else:
            raw = produced
        try:
            return parse_structured(raw, response_model)
        except StructuredOutputError as exc:
            LLM_VALIDATION_FAILURES.labels(
                provider=self.name, schema=response_model.__name__, stage="initial"
            ).inc()
            raise LLMOutputError(details={"reason": exc.reason}) from exc
