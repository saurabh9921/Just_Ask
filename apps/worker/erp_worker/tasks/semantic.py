"""Semantic-layer ingestion task (queue: ``ingestion``)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from celery import shared_task

from app.config import get_settings
from app.core.logging import get_logger
from app.db.session import Database
from app.rag.embeddings import SparseEncoder, build_embedding_provider
from app.rag.ingestion import IngestionDeps, run_ingestion
from app.rag.qdrant_client import VectorStore, build_qdrant_client
from app.services.audit_service import AuditService
from app.workers.jobs import SemanticIngestPayload
from app.workers.queues import TaskName

logger = get_logger(__name__)


async def _run(version_id: uuid.UUID) -> None:
    settings = get_settings()
    database = Database(settings)
    key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    qdrant = build_qdrant_client(settings.qdrant_url, key)
    async with httpx.AsyncClient() as http:
        embedder = build_embedding_provider(settings, http)
        try:
            await run_ingestion(
                IngestionDeps(
                    settings=settings,
                    session_factory=database.session_factory,
                    store=VectorStore(
                        qdrant,
                        prefix=settings.qdrant_collection_prefix,
                        dimensions=embedder.dimensions,
                    ),
                    embedder=embedder,
                    sparse=SparseEncoder(),
                    audit=AuditService(database.session_factory),
                ),
                version_id,
            )
        finally:
            await qdrant.close()
            await database.dispose()


@shared_task(name=TaskName.SEMANTIC_INGEST.value, ignore_result=True)
def ingest_semantic_layer(payload: dict[str, Any]) -> None:
    """Idempotent: ``run_ingestion`` skips versions that are no longer pending."""
    version_id = SemanticIngestPayload.model_validate(payload).version_id
    logger.info("semantic ingestion started", extra={"version_id": str(version_id)})
    asyncio.run(_run(version_id))
