"""Query feedback memory: answers users confirmed as correct become retrieval examples.

A positive rating on a *succeeded* query stores ``Question + validated plan`` in the
``query_feedback`` collection of the connection's active semantic version. The
planner then sees it next to the curated examples (``retrieve_similar_questions``),
so the assistant learns the tenant's phrasing without any model fine-tuning.

What is stored is deliberately limited:

* only the question text and the plan *structure*: filter values (which are data
  values typed by users or taken from the ERP) are removed, as are confidence and
  clarification/refusal fields;
* only plans whose entity is bound to the connection; the document carries the
  entity's required permissions, so retrieval scoping (tenant, connection, active
  version, permissions) applies exactly as for curated examples;
* a negative rating, a changed rating or deleting the query from history removes
  the document; re-ingestion rebuilds the collection from the database
  (newest ``FEEDBACK_MEMORY_MAX_DOCUMENTS`` positive ratings).

The plan is re-validated at use time like any other model output, so a stale
example can never bypass authorization.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import FEEDBACK_MEMORY_UPDATES
from app.db.models import Feedback, QueryRecord, SemanticVersion
from app.rag.chunking import SourceDocument
from app.rag.collections import Collection
from app.semantic.loader import SemanticBundle

logger = get_logger(__name__)

_PLAN_KEYS = (
    "entity",
    "measures",
    "dimensions",
    "date_range",
    "comparison",
    "time_grain",
    "sort",
    "limit",
)


def doc_key(query_id: uuid.UUID) -> str:
    return f"feedback:{query_id}"


def sanitized_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Plan structure without data values; ``None`` if it is not a data plan."""
    if plan.get("refuse") or plan.get("clarification_required") or not plan.get("entity"):
        return None
    return {k: plan[k] for k in _PLAN_KEYS if plan.get(k) not in (None, [], {})}


def feedback_document(
    bundle: SemanticBundle,
    bound: set[str],
    *,
    query_id: uuid.UUID,
    question: str,
    intent: str | None,
    plan: dict[str, Any] | None,
) -> SourceDocument | None:
    clean = sanitized_plan(plan or {})
    if clean is None or clean["entity"] not in bound or clean["entity"] not in bundle.entities:
        return None
    entity = bundle.entities[clean["entity"]]
    text = (
        f"Question: {question}\nIntent: {intent or 'aggregation'}\nEntity: {clean['entity']}\n"
        f"Plan: {json.dumps(clean, sort_keys=True)}\nSource: confirmed by a user"
    )
    return SourceDocument(
        Collection.QUERY_FEEDBACK,
        doc_key(query_id),
        question,
        text,
        clean["entity"],
        tuple(sorted(set(entity.security.required_permissions))),
        {"kind": "feedback"},
    )


async def feedback_documents(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    bundle: SemanticBundle,
    bound: set[str],
    limit: int,
) -> list[SourceDocument]:
    """Documents for the newest positively rated, succeeded, live queries of a connection."""
    if limit <= 0:
        return []
    rows = await session.execute(
        select(QueryRecord.id, QueryRecord.question, QueryRecord.intent, QueryRecord.query_plan)
        .join(Feedback, Feedback.query_id == QueryRecord.id)
        .where(
            QueryRecord.tenant_id == tenant_id,
            QueryRecord.connection_id == connection_id,
            QueryRecord.status == "succeeded",
            QueryRecord.deleted_at.is_(None),
            Feedback.tenant_id == tenant_id,
            Feedback.rating == 1,
        )
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    )
    docs: dict[str, SourceDocument] = {}
    for query_id, question, intent, plan in rows:
        doc = feedback_document(
            bundle, bound, query_id=query_id, question=question, intent=intent, plan=plan
        )
        if doc is not None:
            docs.setdefault(doc.doc_key, doc)
    return list(docs.values())


async def active_version(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> SemanticVersion | None:
    version: SemanticVersion | None = await session.scalar(
        select(SemanticVersion).where(
            SemanticVersion.tenant_id == tenant_id,
            SemanticVersion.connection_id == connection_id,
            SemanticVersion.status == "active",
        )
    )
    return version


def record_update(action: str, outcome: str) -> None:
    FEEDBACK_MEMORY_UPDATES.labels(action, outcome).inc()
