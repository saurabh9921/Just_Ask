"""Semantic-layer ingestion into Qdrant.

``build_documents`` turns a validated semantic bundle, its binding to a
connection's metadata snapshot and the snapshot itself into deterministic
documents for the five collections:

* ``semantic_entities``  - one overview per bound entity + one per measure,
  metric and dimension (fine-grained hits for "revenue" vs "order count").
* ``business_glossary``  - terms and ERP abbreviations whose target is bound.
* ``query_examples``     - example questions whose entity is bound.
* ``database_metadata``  - only the *approved* objects behind bound entities,
  minus forbidden/sensitive columns; comments are sanitised (untrusted input).
* ``query_feedback``     - questions + plan structure users rated as correct
  (``app/rag/feedback_memory.py``), rebuilt from the database on every ingestion.

Unbound entities (object or fields missing in that database) are excluded, so
retrieval never offers vocabulary the connection cannot answer. Forbidden fields
never enter the index at all.

``run_ingestion`` embeds and upserts a new version, activates it in the database,
then deletes every other version for the connection (stale cleanup). Readers
always filter on the active version, so they never see a half-built index.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.common.types import ObjectMetadata
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.metrics import INGESTION_DOCUMENTS, INGESTION_DURATION
from app.core.security import utcnow
from app.db.models import MetadataObject, MetadataSnapshot, SemanticVersion
from app.rag.chunking import SourceDocument, chunk_document, point_id
from app.rag.collections import Collection
from app.rag.embeddings import EmbeddingProvider, SparseEncoder
from app.rag.feedback_memory import feedback_documents
from app.rag.qdrant_client import VectorPoint, VectorStore
from app.rag.text import sanitize_untrusted
from app.semantic.loader import SemanticBundle, SemanticLayerError, load_semantic_layer
from app.semantic.models import Entity
from app.semantic.validator import EntityBinding, bind_to_catalog
from app.services.audit_service import AuditOutcome, AuditService

logger = get_logger(__name__)


# ---------------------------------------------------------------- documents
def _perms(entity: Entity) -> tuple[str, ...]:
    return tuple(sorted(set(entity.security.required_permissions)))


def _entity_documents(bundle: SemanticBundle, entity: Entity) -> list[SourceDocument]:
    perms = _perms(entity)
    name = entity.entity
    metrics = [m for m in bundle.metrics if m.entity == name]
    joins = [j for j in bundle.joins if name in {j.from_entity, j.to_entity}]
    lines = [
        f"Entity {name}: {entity.description}",
        f"Also called: {', '.join(entity.synonyms)}" if entity.synonyms else "",
        "Dimensions: "
        + "; ".join(
            f"{d.name}" + (f" ({', '.join(d.synonyms)})" if d.synonyms else "")
            for d in entity.dimensions
        ),
        "Measures: "
        + "; ".join(
            f"{m.name} [{m.aggregation.value}]"
            + (f" ({', '.join(m.synonyms)})" if m.synonyms else "")
            for m in entity.measures
        ),
        "Metrics: " + "; ".join(m.name for m in metrics) if metrics else "",
        "Time fields: " + ", ".join(t.name for t in entity.time_fields)
        if entity.time_fields
        else "",
        "Related entities: "
        + ", ".join(j.to_entity if j.from_entity == name else j.from_entity for j in joins)
        if joins
        else "",
        "Requires a date filter." if entity.large_fact else "",
    ]
    docs = [
        SourceDocument(
            Collection.SEMANTIC_ENTITIES,
            f"entity:{name}",
            f"{name} (entity)",
            "\n".join(line for line in lines if line),
            name,
            perms,
            {"kind": "entity", "classification": entity.classification.value},
        )
    ]
    for measure in entity.measures:
        text = (
            f"Measure {measure.name} of {name}: {measure.aggregation.value} of {measure.field}. "
            + (measure.description or "")
            + (f" Synonyms: {', '.join(measure.synonyms)}." if measure.synonyms else "")
        )
        docs.append(
            SourceDocument(
                Collection.SEMANTIC_ENTITIES,
                f"measure:{name}.{measure.name}",
                f"{name}.{measure.name} (measure)",
                text.strip(),
                name,
                perms,
                {"kind": "measure"},
            )
        )
    for metric in metrics:
        expr = metric.expression
        text = (
            f"Metric {metric.name} of {name}: {expr.left} {expr.op} {expr.right}. "
            + metric.description
            + (f" Synonyms: {', '.join(metric.synonyms)}." if metric.synonyms else "")
        )
        docs.append(
            SourceDocument(
                Collection.SEMANTIC_ENTITIES,
                f"metric:{name}.{metric.name}",
                f"{name}.{metric.name} (metric)",
                text,
                name,
                perms,
                {"kind": "metric"},
            )
        )
    fields: list[tuple[str, str, str | None, list[str]]] = [
        ("dimension", d.name, d.description, list(d.synonyms)) for d in entity.dimensions
    ] + [("time_field", t.name, t.description, []) for t in entity.time_fields]
    for kind, field_name, description, synonyms in fields:
        text = (
            f"{kind.replace('_', ' ').title()} {field_name} of {name}. "
            + (description or "")
            + (f" Synonyms: {', '.join(synonyms)}." if synonyms else "")
        )
        docs.append(
            SourceDocument(
                Collection.SEMANTIC_ENTITIES,
                f"{kind}:{name}.{field_name}",
                f"{name}.{field_name} ({kind})",
                text.strip(),
                name,
                perms,
                {"kind": kind},
            )
        )
    return docs


def _metadata_document(
    entity: Entity, obj: ObjectMetadata, bundle: SemanticBundle
) -> SourceDocument:
    policy = bundle.policy
    forbidden = {f.lower() for f in entity.security.forbidden_fields}
    flagged = False
    columns: list[str] = []
    for col in obj.columns:
        if col.name.lower() in forbidden or policy.is_forbidden(col.name):
            continue  # never indexed, never shown to a model
        comment, bad = sanitize_untrusted(col.comment, max_length=200)
        flagged = flagged or bad
        columns.append(
            f"{col.name} {col.canonical_type.value}" + (f" -- {comment}" if comment else "")
        )
    comment, bad = sanitize_untrusted(obj.comment, max_length=300)
    flagged = flagged or bad
    text = (
        f"Database {obj.kind.value} {obj.schema_name}.{obj.name} backs entity {entity.entity}. "
        + (f"Comment: {comment}. " if comment else "")
        + "Columns: "
        + "; ".join(columns)
    )
    return SourceDocument(
        Collection.DATABASE_METADATA,
        f"object:{obj.schema_name}.{obj.name}",
        f"{obj.schema_name}.{obj.name}",
        text,
        entity.entity,
        _perms(entity),
        {"kind": obj.kind.value, "untrusted_flagged": flagged},
    )


def build_documents(
    bundle: SemanticBundle,
    bindings: dict[str, EntityBinding],
    objects: list[ObjectMetadata],
) -> list[SourceDocument]:
    bound = {name for name, b in bindings.items() if b.bound}
    docs: list[SourceDocument] = []
    for name in sorted(bound):
        docs.extend(_entity_documents(bundle, bundle.entities[name]))

    for term in sorted(bundle.terms, key=lambda t: t.term.lower()):
        target = term.maps_to
        if target is not None and target.entity not in bound:
            continue
        perms = _perms(bundle.entities[target.entity]) if target else ("analytics.query",)
        mapping = ""
        if target:
            field = target.measure or target.metric or target.dimension
            mapping = f" Maps to {target.entity}" + (f".{field}" if field else "") + "."
        text = (
            f"{term.term}: {term.definition}"
            + (f" Synonyms: {', '.join(term.synonyms)}." if term.synonyms else "")
            + mapping
        )
        docs.append(
            SourceDocument(
                Collection.BUSINESS_GLOSSARY,
                f"term:{term.term.lower()}",
                term.term,
                text,
                target.entity if target else None,
                perms,
                {"kind": "term"},
            )
        )
    for abbr in sorted(bundle.abbreviations, key=lambda a: a.abbreviation.lower()):
        docs.append(
            SourceDocument(
                Collection.BUSINESS_GLOSSARY,
                f"abbr:{abbr.abbreviation.lower()}",
                abbr.abbreviation,
                f"{abbr.abbreviation} means {abbr.expansion}.",
                None,
                ("analytics.query",),
                {"kind": "abbreviation"},
            )
        )

    for ex in sorted(bundle.examples, key=lambda e: e.id):
        if ex.entity not in bound:
            continue
        text = (
            f"Question: {ex.question}\nIntent: {ex.intent}\nEntity: {ex.entity}\n"
            f"Plan: {json.dumps(ex.plan, sort_keys=True)}"
            + (f"\nNotes: {ex.notes}" if ex.notes else "")
        )
        docs.append(
            SourceDocument(
                Collection.QUERY_EXAMPLES,
                f"example:{ex.id}",
                ex.question,
                text,
                ex.entity,
                _perms(bundle.entities[ex.entity]),
                {"kind": "example", "intent": ex.intent},
            )
        )

    by_key = {(o.schema_name, o.name): o for o in objects}
    indexed: set[tuple[str, str]] = set()
    for name in sorted(bound):
        binding = bindings[name]
        key = (binding.schema_name or "", binding.object_name or "")
        obj = by_key.get(key)
        if obj is not None and key not in indexed:
            indexed.add(key)
            docs.append(_metadata_document(bundle.entities[name], obj, bundle))
    return docs


# ---------------------------------------------------------------- ingestion
@dataclass(slots=True)
class IngestionDeps:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    store: VectorStore
    embedder: EmbeddingProvider
    sparse: SparseEncoder
    audit: AuditService


class _IngestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _latest_catalog(
    session: AsyncSession, connection_id: uuid.UUID
) -> tuple[uuid.UUID, list[ObjectMetadata]]:
    snapshot = await session.scalar(
        select(MetadataSnapshot)
        .where(
            MetadataSnapshot.connection_id == connection_id, MetadataSnapshot.status == "succeeded"
        )
        .order_by(MetadataSnapshot.version.desc())
        .limit(1)
    )
    if snapshot is None:
        raise _IngestError("METADATA_SYNC_REQUIRED")
    rows = await session.scalars(
        select(MetadataObject).where(MetadataObject.snapshot_id == snapshot.id)
    )
    return snapshot.id, [ObjectMetadata.model_validate(r.definition) for r in rows]


async def _embed_and_upsert(
    deps: IngestionDeps,
    docs: list[SourceDocument],
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    version: int,
) -> Counter[str]:
    chunks = [c for d in docs for c in chunk_document(d)]
    counts: Counter[str] = Counter()
    batch = deps.settings.embedding_batch_size
    for start in range(0, len(chunks), batch):
        part = chunks[start : start + batch]
        vectors = await deps.embedder.embed_documents(
            [f"{c.document.title}\n{c.text}" for c in part]
        )
        by_collection: dict[Collection, list[VectorPoint]] = {}
        for chunk, vector in zip(part, vectors, strict=True):
            doc = chunk.document
            indices, values = deps.sparse.encode(f"{doc.title}\n{chunk.text}")
            by_collection.setdefault(doc.collection, []).append(
                VectorPoint(
                    id=point_id(
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        chunk=chunk,
                        version=version,
                    ),
                    dense=vector,
                    sparse_indices=indices,
                    sparse_values=values,
                    payload={
                        "tenant_id": str(tenant_id),
                        "connection_id": str(connection_id),
                        "version": version,
                        "document_type": doc.collection.value,
                        "doc_key": doc.doc_key,
                        "title": doc.title,
                        "text": chunk.text,
                        "chunk_index": chunk.index,
                        "entity_name": doc.entity_name,
                        "permissions": list(doc.permissions),
                        **doc.extra,
                    },
                )
            )
        for collection, points in by_collection.items():
            await deps.store.upsert(
                collection, points, batch_size=deps.settings.qdrant_upsert_batch_size
            )
            counts[collection.value] += len(points)
            INGESTION_DOCUMENTS.labels(collection=collection.value).inc(len(points))
    return counts


async def upsert_documents(
    deps: IngestionDeps,
    docs: list[SourceDocument],
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    version: int,
) -> Counter[str]:
    """Embed and upsert documents into an existing version (e.g. feedback memory)."""
    await deps.store.ensure()
    return await _embed_and_upsert(
        deps, docs, tenant_id=tenant_id, connection_id=connection_id, version=version
    )


async def _feedback_docs(
    deps: IngestionDeps,
    session: AsyncSession,
    row: SemanticVersion,
    bundle: SemanticBundle,
    bindings: dict[str, EntityBinding],
) -> list[SourceDocument]:
    if not deps.settings.feedback_memory_enabled:
        return []
    return await feedback_documents(
        session,
        tenant_id=row.tenant_id,
        connection_id=row.connection_id,
        bundle=bundle,
        bound={name for name, b in bindings.items() if b.bound},
        limit=deps.settings.feedback_memory_max_documents,
    )


async def run_ingestion(deps: IngestionDeps, version_id: uuid.UUID) -> None:
    """Build, activate and clean up one semantic version. Idempotent for non-pending rows."""
    started = time.perf_counter()
    async with deps.session_factory() as session:
        row = await session.get(SemanticVersion, version_id)
        if row is None or row.status != "pending":
            return
        row.status = "building"
        row.started_at = utcnow()
        await session.commit()
        tenant_id, connection_id, version = row.tenant_id, row.connection_id, row.version

        outcome = "failed"
        try:
            bundle = load_semantic_layer(deps.settings.semantic_layer_path)
            snapshot_id, objects = await _latest_catalog(session, connection_id)
            bindings = bind_to_catalog(bundle, objects)
            if not any(b.bound for b in bindings.values()):
                raise _IngestError("NO_ENTITY_BOUND")  # noqa: TRY301 - maps to a status code
            docs = build_documents(bundle, bindings, objects) + await _feedback_docs(
                deps, session, row, bundle, bindings
            )
            await deps.store.ensure()
            counts = await _embed_and_upsert(
                deps, docs, tenant_id=tenant_id, connection_id=connection_id, version=version
            )
            # Activate atomically: supersede the previous active version, then activate this one.
            await session.execute(
                update(SemanticVersion)
                .where(
                    SemanticVersion.connection_id == connection_id,
                    SemanticVersion.status == "active",
                )
                .values(status="superseded")
            )
            row.status = "active"
            row.activated_at = utcnow()
            row.content_hash = bundle.content_hash
            row.metadata_snapshot_id = snapshot_id
            row.embedding_model = deps.embedder.name
            row.embedding_dimensions = deps.embedder.dimensions
            row.document_counts = dict(counts)
            row.entity_bindings = {
                name: {"bound": b.bound, "object": b.object_name, "problems": b.problems[:10]}
                for name, b in sorted(bindings.items())
            }
            row.finished_at = utcnow()
            await session.commit()
            outcome = "succeeded"
            # Stale cleanup only after the switch, so readers never hit an empty index.
            await deps.store.delete_other_versions(
                tenant_id=tenant_id, connection_id=connection_id, keep_version=version
            )
        except Exception as exc:  # noqa: BLE001 - any failure must end in a terminal state
            await session.rollback()
            code = _error_code(exc)
            logger.warning(
                "semantic ingestion failed",
                extra={
                    "version_id": str(version_id),
                    "error_code": code,
                    "error_type": type(exc).__name__,
                },
            )
            failed = await session.get(SemanticVersion, version_id)
            if failed is not None:
                failed.status = "failed"
                failed.error_code = code[:64]
                failed.finished_at = utcnow()
                await session.commit()
            try:
                await deps.store.delete_version(
                    tenant_id=tenant_id, connection_id=connection_id, version=version
                )
            except Exception:  # noqa: BLE001 - collections may not exist yet
                logger.info("no partial vectors to remove", extra={"version_id": str(version_id)})
        finally:
            INGESTION_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)

    await deps.audit.record(
        "semantic.ingestion_completed",
        AuditOutcome.SUCCESS if outcome == "succeeded" else AuditOutcome.ERROR,
        tenant_id=tenant_id,
        resource_type="connection",
        resource_id=str(connection_id),
        details={"version": version},
    )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, _IngestError):
        return exc.code
    if isinstance(exc, SemanticLayerError):
        return "SEMANTIC_LAYER_INVALID"
    if isinstance(exc, AppError):
        return exc.code
    return "VECTOR_STORE_ERROR" if "qdrant" in type(exc).__module__ else "INGESTION_ERROR"
