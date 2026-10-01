"""Vector store access with *mandatory* scoping.

Every read goes through :class:`RetrievalScope`, which always filters by tenant,
connection, active semantic version and the caller's permissions. There is no
unscoped search method, so a caller cannot forget a filter.

Permission filter: a document lists the permissions it requires; it matches only
if *all* of them are held. Qdrant has no "subset of" operator, so this is
expressed as ``must_not permissions ∈ (ALL_PERMISSIONS - held)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse

from app.core.exceptions import AppError
from app.core.permissions import Permission
from app.rag.collections import DENSE, SPARSE, Collection, ensure_collections, physical_name

ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)


class VectorIndexMissingError(AppError):
    """The database says an index is active but its collection is gone (e.g. lost volume)."""

    status_code = 409
    code = "SEMANTIC_INDEX_NOT_READY"
    default_message = (
        "The semantic index for this data source is unavailable. Ask a data admin to rebuild it."
    )


def _missing_collection(exc: Exception) -> bool:
    if isinstance(exc, UnexpectedResponse):
        return exc.status_code == 404
    return isinstance(exc, ValueError) and "not found" in str(exc).lower()


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    version: int
    permissions: frozenset[str]

    def to_filter(self) -> qm.Filter:
        must: list[qm.Condition] = [
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(self.tenant_id))),
            qm.FieldCondition(
                key="connection_id", match=qm.MatchValue(value=str(self.connection_id))
            ),
            qm.FieldCondition(key="version", match=qm.MatchValue(value=self.version)),
        ]
        missing = sorted(ALL_PERMISSIONS - self.permissions)
        must_not: list[qm.Condition] = (
            [qm.FieldCondition(key="permissions", match=qm.MatchAny(any=missing))]
            if missing
            else []
        )
        return qm.Filter(must=must, must_not=must_not)


@dataclass(frozen=True, slots=True)
class VectorPoint:
    id: str
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScoredPoint:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStore:
    def __init__(self, client: AsyncQdrantClient, *, prefix: str, dimensions: int) -> None:
        self.client = client
        self.prefix = prefix
        self.dimensions = dimensions

    def name(self, collection: Collection) -> str:
        return physical_name(self.prefix, collection)

    async def ensure(self) -> None:
        await ensure_collections(self.client, prefix=self.prefix, dimensions=self.dimensions)

    async def upsert(
        self, collection: Collection, points: Sequence[VectorPoint], *, batch_size: int
    ) -> None:
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            await self.client.upsert(
                self.name(collection),
                points=[
                    qm.PointStruct(
                        id=p.id,
                        vector={
                            DENSE: p.dense,
                            SPARSE: qm.SparseVector(
                                indices=p.sparse_indices, values=p.sparse_values
                            ),
                        },
                        payload=p.payload,
                    )
                    for p in batch
                ],
                wait=True,
            )

    async def delete_other_versions(
        self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, keep_version: int | None
    ) -> None:
        """Remove every version but ``keep_version`` (``None`` removes all) for one connection."""
        must: list[qm.Condition] = [
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
            qm.FieldCondition(key="connection_id", match=qm.MatchValue(value=str(connection_id))),
        ]
        must_not: list[qm.Condition] = (
            [qm.FieldCondition(key="version", match=qm.MatchValue(value=keep_version))]
            if keep_version is not None
            else []
        )
        selector = qm.FilterSelector(filter=qm.Filter(must=must, must_not=must_not))
        for collection in Collection:
            await self.client.delete(self.name(collection), points_selector=selector, wait=True)

    async def delete_version(
        self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, version: int
    ) -> None:
        selector = qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
                    qm.FieldCondition(
                        key="connection_id", match=qm.MatchValue(value=str(connection_id))
                    ),
                    qm.FieldCondition(key="version", match=qm.MatchValue(value=version)),
                ]
            )
        )
        for collection in Collection:
            await self.client.delete(self.name(collection), points_selector=selector, wait=True)

    async def delete_document(
        self,
        collection: Collection,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        doc_key: str,
    ) -> None:
        """Remove every chunk of one document (all versions) for one connection."""
        selector = qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
                    qm.FieldCondition(
                        key="connection_id", match=qm.MatchValue(value=str(connection_id))
                    ),
                    qm.FieldCondition(key="doc_key", match=qm.MatchValue(value=doc_key)),
                ]
            )
        )
        await self.client.delete(self.name(collection), points_selector=selector, wait=True)

    async def count(self, collection: Collection, scope: RetrievalScope) -> int:
        result = await self.client.count(
            self.name(collection), count_filter=scope.to_filter(), exact=True
        )
        return int(result.count)

    async def _query(
        self,
        collection: Collection,
        scope: RetrievalScope,
        *,
        query: list[float] | qm.SparseVector,
        using: str,
        limit: int,
    ) -> qm.QueryResponse:
        try:
            return await self.client.query_points(
                self.name(collection),
                query=query,
                using=using,
                query_filter=scope.to_filter(),
                limit=limit,
                with_payload=True,
            )
        except (ValueError, UnexpectedResponse) as exc:
            if _missing_collection(exc):
                raise VectorIndexMissingError() from exc
            raise

    async def search_dense(
        self, collection: Collection, scope: RetrievalScope, vector: list[float], limit: int
    ) -> list[ScoredPoint]:
        response = await self._query(collection, scope, query=vector, using=DENSE, limit=limit)
        return [
            ScoredPoint(str(p.id), float(p.score), dict(p.payload or {})) for p in response.points
        ]

    async def search_sparse(
        self,
        collection: Collection,
        scope: RetrievalScope,
        indices: list[int],
        values: list[float],
        limit: int,
    ) -> list[ScoredPoint]:
        if not indices:
            return []
        response = await self._query(
            collection,
            scope,
            query=qm.SparseVector(indices=indices, values=values),
            using=SPARSE,
            limit=limit,
        )
        return [
            ScoredPoint(str(p.id), float(p.score), dict(p.payload or {})) for p in response.points
        ]


def build_qdrant_client(url: str, api_key: str | None) -> AsyncQdrantClient:
    """``QDRANT_URL=:memory:`` gives an in-process store (tests / Docker-less development)."""
    if url == ":memory:":
        return AsyncQdrantClient(location=":memory:")
    # No construction-time version probe: it is a blocking network call at startup and
    # readiness (/health/ready) already reports Qdrant availability.
    return AsyncQdrantClient(
        url=url, api_key=api_key, timeout=10, prefer_grpc=False, check_compatibility=False
    )
