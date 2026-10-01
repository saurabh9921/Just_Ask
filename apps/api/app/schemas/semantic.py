"""Semantic ingestion, retrieval and entity-resolution models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.agents.schemas import EntityResolution, IntentClassification
from app.rag.collections import Collection
from app.schemas.common import ResponseModel, StrictModel

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class SemanticVersionOut(ResponseModel):
    version_id: uuid.UUID
    version: int
    status: str
    content_hash: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    document_counts: dict[str, Any]
    entity_bindings: dict[str, Any]
    error_code: str | None
    created_at: datetime
    activated_at: datetime | None
    finished_at: datetime | None


class SemanticStatusOut(ResponseModel):
    connection_id: uuid.UUID
    latest: SemanticVersionOut | None
    active_version: int | None


class SearchRequest(StrictModel):
    query: Question
    collections: list[Collection] = Field(
        default_factory=lambda: [
            Collection.SEMANTIC_ENTITIES,
            Collection.BUSINESS_GLOSSARY,
            Collection.QUERY_EXAMPLES,
            Collection.DATABASE_METADATA,
        ],
        min_length=1,
        max_length=5,
    )
    top_k: int = Field(default=5, ge=1, le=20)


class SearchHit(ResponseModel):
    collection: str
    doc_key: str
    title: str
    text: str
    entity_name: str | None
    score: float
    dense_rank: int | None
    sparse_rank: int | None
    untrusted_flagged: bool


class SearchResponse(ResponseModel):
    version: int
    hits: list[SearchHit]


class ResolveRequest(StrictModel):
    question: Question


class ResolveResponse(ResponseModel):
    version: int
    intent: IntentClassification
    resolution: EntityResolution
    rejected_names: list[str] = Field(
        description="Names the model produced that are not in the governed vocabulary"
    )
    context: list[str] = Field(description="doc_keys of the retrieved context given to the model")
    llm_provider: str
