"""Semantic ingestion requests, scoped retrieval and LLM entity resolution.

``resolve`` is the Phase 3 end-to-end path (retrieve → prompt → structured
LLM output → re-validation). The model's answer is never trusted: the entity
must be in the governed, bound, permitted vocabulary, and every measure,
dimension and time field must belong to that entity; anything else is dropped
and reported in ``rejected_names``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts.templates import entity_resolution_messages, intent_classification_messages
from app.agents.schemas import EntityResolution, IntentClassification
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import MetadataSnapshot, SemanticVersion
from app.llm.base import LLMProvider
from app.rag.collections import Collection
from app.rag.hybrid_retriever import HybridRetriever, RetrievedDocument
from app.rag.qdrant_client import RetrievalScope
from app.schemas.semantic import SemanticStatusOut, SemanticVersionOut
from app.semantic.loader import SemanticBundle
from app.semantic.registry import SemanticLayerRegistry
from app.services.audit_service import AuditOutcome, AuditService
from app.services.connection_service import ConnectionService
from app.workers.jobs import is_abandoned

logger = get_logger(__name__)

ACTIVE_BUILD_STATUSES = ("pending", "building")


class SemanticIndexNotReadyError(AppError):
    status_code = 409
    code = "SEMANTIC_INDEX_NOT_READY"
    default_message = "The semantic index for this connection has not been built yet."


class MetadataSyncRequiredError(AppError):
    status_code = 409
    code = "METADATA_SYNC_REQUIRED"
    default_message = "Run a metadata sync for this connection before building the semantic index."


def to_version_out(row: SemanticVersion) -> SemanticVersionOut:
    return SemanticVersionOut(
        version_id=row.id,
        version=row.version,
        status=row.status,
        content_hash=row.content_hash,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        document_counts=row.document_counts,
        entity_bindings=row.entity_bindings,
        error_code=row.error_code,
        created_at=row.created_at,
        activated_at=row.activated_at,
        finished_at=row.finished_at,
    )


@dataclass(slots=True)
class SemanticService:
    session: AsyncSession
    ctx: TenantContext
    connections: ConnectionService
    audit: AuditService
    retriever: HybridRetriever
    registry: SemanticLayerRegistry
    llm: LLMProvider
    top_k: int

    # ------------------------------------------------------------ ingestion
    async def request_ingest(self, connection_id: uuid.UUID) -> tuple[SemanticVersion, bool]:
        self.ctx.require(Permission.CONNECTIONS_MANAGE)
        conn = await self.connections.get(connection_id)
        has_snapshot = await self.session.scalar(
            select(func.count())
            .select_from(MetadataSnapshot)
            .where(
                MetadataSnapshot.connection_id == conn.id, MetadataSnapshot.status == "succeeded"
            )
        )
        if not has_snapshot:
            raise MetadataSyncRequiredError()
        in_flight = await self.session.scalar(
            select(SemanticVersion)
            .where(
                SemanticVersion.connection_id == conn.id,
                SemanticVersion.status.in_(ACTIVE_BUILD_STATUSES),
            )
            .limit(1)
        )
        if in_flight is not None:
            if not is_abandoned(in_flight.created_at, in_flight.started_at):
                return in_flight, False
            in_flight.status = "failed"
            in_flight.error_code = "ABANDONED"
            in_flight.finished_at = utcnow()
            logger.warning(
                "abandoned semantic build superseded",
                extra={"version_id": str(in_flight.id), "connection_id": str(conn.id)},
            )
        current = await self.session.scalar(
            select(func.max(SemanticVersion.version)).where(
                SemanticVersion.connection_id == conn.id
            )
        )
        row = SemanticVersion(
            id=uuid.uuid4(),
            tenant_id=self.ctx.tenant_id,
            connection_id=conn.id,
            version=(current or 0) + 1,
            status="pending",
            requested_by=self.ctx.user_id,
            document_counts={},
            entity_bindings={},
        )
        self.session.add(row)
        await self.session.commit()
        await self.audit.record(
            "semantic.ingestion_requested",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="connection",
            resource_id=str(conn.id),
            details={"version": row.version},
        )
        return row, True

    async def _active(self, connection_id: uuid.UUID) -> SemanticVersion | None:
        row: SemanticVersion | None = await self.session.scalar(
            select(SemanticVersion).where(
                SemanticVersion.connection_id == connection_id,
                SemanticVersion.tenant_id == self.ctx.tenant_id,
                SemanticVersion.status == "active",
            )
        )
        return row

    async def status(self, connection_id: uuid.UUID) -> SemanticStatusOut:
        self.ctx.require(Permission.METADATA_READ)
        conn = await self.connections.get(connection_id)
        latest = await self.session.scalar(
            select(SemanticVersion)
            .where(
                SemanticVersion.connection_id == conn.id,
                SemanticVersion.tenant_id == self.ctx.tenant_id,
            )
            .order_by(SemanticVersion.version.desc())
            .limit(1)
        )
        active = await self._active(conn.id)
        return SemanticStatusOut(
            connection_id=conn.id,
            latest=to_version_out(latest) if latest else None,
            active_version=active.version if active else None,
        )

    # ------------------------------------------------------------ retrieval
    async def _scope(self, connection_id: uuid.UUID) -> tuple[RetrievalScope, SemanticVersion]:
        conn = await self.connections.get(connection_id)  # visibility + tenant check
        active = await self._active(conn.id)
        if active is None:
            raise SemanticIndexNotReadyError()
        scope = RetrievalScope(
            tenant_id=self.ctx.tenant_id,
            connection_id=conn.id,
            version=active.version,
            permissions=frozenset(p.value for p in self.ctx.permissions),
        )
        return scope, active

    async def search(
        self, connection_id: uuid.UUID, query: str, collections: list[Collection], top_k: int
    ) -> tuple[int, list[RetrievedDocument]]:
        self.ctx.require(Permission.METADATA_READ)
        scope, active = await self._scope(connection_id)
        return active.version, await self.retriever.search(
            scope, query, collections=collections, top_k=top_k
        )

    # ----------------------------------------------------------- resolution
    def _vocabulary(
        self, bundle: SemanticBundle, active: SemanticVersion
    ) -> dict[str, dict[str, list[str]]]:
        held = {p.value for p in self.ctx.permissions}
        vocab: dict[str, dict[str, list[str]]] = {}
        for name, entity in bundle.entities.items():
            binding = active.entity_bindings.get(name, {})
            if not binding.get("bound"):
                continue
            if not set(entity.security.required_permissions) <= held:
                continue
            metrics = [m.name for m in bundle.metrics if m.entity == name]
            vocab[name] = {
                "measures": [m.name for m in entity.measures] + metrics,
                "dimensions": [d.name for d in entity.dimensions],
                "time_fields": [t.name for t in entity.time_fields],
            }
        return vocab

    @staticmethod
    def _validate(
        raw: EntityResolution, vocab: dict[str, dict[str, list[str]]]
    ) -> tuple[EntityResolution, list[str]]:
        rejected: list[str] = []
        if raw.entity is None:
            return raw, rejected
        fields = vocab.get(raw.entity)
        if fields is None:
            rejected.append(raw.entity)
            names = [*raw.measures, *raw.dimensions, *([raw.time_field] if raw.time_field else [])]
            rejected.extend(names)
            return (
                EntityResolution(
                    entity=None, confidence=0.0, unresolved_terms=raw.unresolved_terms
                ),
                rejected,
            )
        measures = [m for m in raw.measures if m in fields["measures"]]
        dimensions = [d for d in raw.dimensions if d in fields["dimensions"]]
        time_field = raw.time_field if raw.time_field in fields["time_fields"] else None
        rejected.extend(m for m in raw.measures if m not in fields["measures"])
        rejected.extend(d for d in raw.dimensions if d not in fields["dimensions"])
        if raw.time_field and time_field is None:
            rejected.append(raw.time_field)
        return (
            EntityResolution(
                entity=raw.entity,
                measures=measures,
                dimensions=dimensions,
                time_field=time_field,
                confidence=raw.confidence if not rejected else min(raw.confidence, 0.5),
                unresolved_terms=raw.unresolved_terms,
            ),
            rejected,
        )

    async def resolve(self, connection_id: uuid.UUID, question: str) -> dict[str, object]:
        self.ctx.require(Permission.ANALYTICS_QUERY)
        scope, active = await self._scope(connection_id)
        bundle = self.registry.get()
        vocab = self._vocabulary(bundle, active)
        intent = await self.llm.generate_structured(
            intent_classification_messages(question), IntentClassification
        )
        docs = await self.retriever.search(
            scope,
            question,
            collections=[
                Collection.SEMANTIC_ENTITIES,
                Collection.BUSINESS_GLOSSARY,
                Collection.QUERY_EXAMPLES,
            ],
            top_k=self.top_k,
        )
        raw = await self.llm.generate_structured(
            entity_resolution_messages(question, docs, vocab), EntityResolution
        )
        resolution, rejected = self._validate(raw, vocab)
        return {
            "version": active.version,
            "intent": intent,
            "resolution": resolution,
            "rejected_names": rejected,
            "context": [d.doc_key for d in docs],
            "llm_provider": self.llm.name,
        }
