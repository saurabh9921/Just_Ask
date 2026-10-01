"""LLM-backed query planning with deterministic support code.

* ``build_vocabulary`` - the governed names the model may use: bound entities the
  caller is permitted to query, minus sensitive fields, plus related entities
  reachable through approved many-to-one joins.
* ``QueryPlanner`` - prompts the model for a :class:`QueryPlan` and, when the
  validator rejects it for retryable reasons, makes exactly one correction call.
* ``describe_plan`` - a deterministic plain-English description of what will be
  (or was) queried; this, not model text, is what the UI shows as "query plan".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from app.agents.prompts.planning import plan_correction_messages, query_plan_messages
from app.llm.base import ChatMessage, LLMProvider
from app.query.plans import QueryPlan
from app.query.validator import ResolvedMeasure, ResolvedPlan
from app.rag.hybrid_retriever import RetrievedDocument
from app.semantic.loader import SemanticBundle
from app.semantic.validator import EntityBinding


def build_vocabulary(
    bundle: SemanticBundle, bindings: dict[str, EntityBinding], permissions: frozenset[str]
) -> dict[str, dict[str, Any]]:
    usable = {
        name
        for name, entity in bundle.entities.items()
        if bindings.get(name) is not None
        and bindings[name].bound
        and set(entity.security.required_permissions) <= permissions
    }
    vocab: dict[str, dict[str, Any]] = {}
    for name in sorted(usable):
        entity = bundle.entities[name]
        related: list[str] = []
        for join in bundle.joins:
            if join.from_entity == name and join.relationship in {"many_to_one", "one_to_one"}:
                other = join.to_entity
            elif join.to_entity == name and join.relationship in {"one_to_many", "one_to_one"}:
                other = join.from_entity
            else:
                continue
            if other in usable:
                related += [
                    f"{other}.{d.name}"
                    for d in bundle.entities[other].dimensions
                    if not d.sensitive
                ]
        vocab[name] = {
            "description": entity.description,
            "measures": [m.name for m in entity.measures]
            + [m.name for m in bundle.metrics if m.entity == name],
            "dimensions": [d.name for d in entity.dimensions if not d.sensitive],
            "time_fields": [t.name for t in entity.time_fields],
            "related": related[:40],
        }
    return vocab


class QueryPlanner:
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    async def build(
        self,
        question: str,
        docs: Sequence[RetrievedDocument],
        vocabulary: dict[str, dict[str, Any]],
        today: date,
        previous_plan: dict[str, Any] | None = None,
    ) -> tuple[QueryPlan, list[ChatMessage]]:
        messages = query_plan_messages(question, docs, vocabulary, today, previous_plan)
        plan = await self.llm.generate_structured(messages, QueryPlan)
        return plan, messages

    async def correct(
        self, base_messages: list[ChatMessage], rejected: QueryPlan, issues: list[str]
    ) -> QueryPlan:
        messages = plan_correction_messages(
            base_messages, rejected.model_dump(mode="json", exclude_defaults=True), issues
        )
        return await self.llm.generate_structured(messages, QueryPlan)


_AGG = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "distinct count",
    "avg": "average",
    "min": "minimum",
    "max": "maximum",
}


def _describe_measure(m: ResolvedMeasure) -> str:
    label = m.label.replace("_", " ")
    if m.op is None and m.aggregation and m.field:
        return f"{label} ({_AGG.get(m.aggregation, m.aggregation)} of {m.field})"
    return label


def describe_plan(resolved: ResolvedPlan) -> str:
    parts = [", ".join(_describe_measure(m) for m in resolved.measures) or "records"]
    if resolved.dimensions:
        dims = []
        for d in resolved.dimensions:
            text = d.ref.replace("_", " ")
            if d.time_grain:
                text += f" ({d.time_grain.value})"
            dims.append(text)
        parts.append("by " + ", ".join(dims))
    parts.append(f"from {resolved.entity}")
    if resolved.time_filter is not None:
        tf = resolved.time_filter
        parts.append(f"for {tf.ref.replace('_', ' ')} {tf.window.label()}")
        if tf.previous is not None:
            parts.append(f"compared with {tf.previous.label()}")
    if resolved.filters:
        conditions = []
        for f in resolved.filters:
            value = f.value if not isinstance(f.value, list) else ", ".join(map(str, f.value))
            conditions.append(f"{f.ref} {f.op.value.replace('_', ' ')} {value}".strip())
        parts.append("where " + " and ".join(conditions))
    if resolved.sort:
        name, direction = resolved.sort[0]
        order = "descending" if direction == "desc" else "ascending"
        parts.append(f"sorted by {name.replace('_', ' ')} {order}")
    parts.append(f"(max {resolved.limit} rows)")
    return " ".join(parts)
