"""Hybrid retrieval: dense + sparse per collection, fused with Reciprocal Rank Fusion.

RRF is computed client-side so the result is deterministic and independent of the
Qdrant version: ``score(d) = Σ 1 / (k + rank_i(d))`` over the dense and sparse
lists, ties broken by document key. Chunks are de-duplicated per document.
An optional reranker then reorders the fused candidates.

Results are cached in Redis under a key that includes tenant, connection,
semantic version and a hash of the caller's permissions, so a cached answer can
never be served to a user with a different authorization scope.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.core.metrics import CACHE_REQUESTS, RETRIEVAL_DURATION
from app.rag.collections import Collection
from app.rag.embeddings import EmbeddingProvider, SparseEncoder
from app.rag.qdrant_client import RetrievalScope, ScoredPoint, VectorStore
from app.rag.reranker import Reranker

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    collection: str
    doc_key: str
    title: str
    text: str
    entity_name: str | None
    score: float
    dense_rank: int | None
    sparse_rank: int | None
    untrusted_flagged: bool


def rrf_fuse(
    ranked_lists: list[list[ScoredPoint]], *, k: int
) -> list[tuple[ScoredPoint, float, list[int | None]]]:
    """Fuse ranked lists by document (``doc_key``); returns (best point, score, ranks)."""
    scores: dict[str, float] = {}
    best: dict[str, ScoredPoint] = {}
    ranks: dict[str, list[int | None]] = {}
    for list_index, points in enumerate(ranked_lists):
        seen: set[str] = set()
        rank = 0
        for point in points:
            key = str(point.payload.get("doc_key", point.id))
            if key in seen:  # multiple chunks of one document: count its best rank once
                continue
            seen.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            best.setdefault(key, point)
            ranks.setdefault(key, [None] * len(ranked_lists))[list_index] = rank
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    return [(best[key], scores[key], ranks[key]) for key in ordered]


class HybridRetriever:
    def __init__(
        self,
        *,
        store: VectorStore,
        embedder: EmbeddingProvider,
        sparse: SparseEncoder,
        reranker: Reranker | None,
        redis: Redis | None,
        candidate_k: int,
        rrf_k: int,
        cache_ttl_seconds: int,
        max_query_chars: int,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._sparse = sparse
        self._reranker = reranker
        self._redis = redis
        self._candidate_k = candidate_k
        self._rrf_k = rrf_k
        self._ttl = cache_ttl_seconds
        self._max_query_chars = max_query_chars

    @staticmethod
    def cache_key(
        scope: RetrievalScope, query: str, collections: list[Collection], top_k: int, reranker: str
    ) -> str:
        material = json.dumps(
            {
                "q": " ".join(query.lower().split()),
                "c": sorted(c.value for c in collections),
                "k": top_k,
                "p": sorted(scope.permissions),
                "r": reranker,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(material.encode()).hexdigest()[:40]
        return f"rag:{scope.tenant_id}:{scope.connection_id}:v{scope.version}:{digest}"

    async def invalidate_connection(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> int:
        """Drop cached retrievals of one connection (after its documents changed)."""
        if self._redis is None:
            return 0
        removed = 0
        try:
            batch: list[str] = []
            async for key in self._redis.scan_iter(
                match=f"rag:{tenant_id}:{connection_id}:*", count=500
            ):
                batch.append(key)
                if len(batch) >= 500:
                    removed += await self._redis.delete(*batch)
                    batch = []
            if batch:
                removed += await self._redis.delete(*batch)
        except (RedisError, OSError):
            logger.warning("retrieval cache invalidation failed; entries expire by TTL")
        return removed

    async def _cache_get(self, key: str) -> list[RetrievedDocument] | None:
        if self._redis is None or self._ttl <= 0:
            return None
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError):
            CACHE_REQUESTS.labels(cache="retrieval", result="error").inc()
            return None
        if raw is None:
            CACHE_REQUESTS.labels(cache="retrieval", result="miss").inc()
            return None
        CACHE_REQUESTS.labels(cache="retrieval", result="hit").inc()
        return [RetrievedDocument(**item) for item in json.loads(raw)]

    async def _cache_set(self, key: str, docs: list[RetrievedDocument]) -> None:
        if self._redis is None or self._ttl <= 0:
            return
        try:
            await self._redis.set(key, json.dumps([asdict(d) for d in docs]), ex=self._ttl)
        except (RedisError, OSError):
            logger.warning("retrieval cache write failed")

    async def search(
        self,
        scope: RetrievalScope,
        query: str,
        *,
        collections: list[Collection],
        top_k: int,
    ) -> list[RetrievedDocument]:
        query = query.strip()
        if not query or len(query) > self._max_query_chars:
            raise BadRequestError(
                "The query is empty or too long.", details={"reason": "QUERY_LENGTH"}
            )
        started = time.perf_counter()
        key = self.cache_key(
            scope, query, collections, top_k, self._reranker.name if self._reranker else "none"
        )
        cached = await self._cache_get(key)
        if cached is not None:
            RETRIEVAL_DURATION.labels(cache="hit").observe(time.perf_counter() - started)
            return cached

        dense = await self._embedder.embed_query(query)
        indices, values = self._sparse.encode(query, query=True)
        results: list[RetrievedDocument] = []
        for collection in collections:
            dense_hits = await self._store.search_dense(collection, scope, dense, self._candidate_k)
            sparse_hits = await self._store.search_sparse(
                collection, scope, indices, values, self._candidate_k
            )
            fused = rrf_fuse([dense_hits, sparse_hits], k=self._rrf_k)
            docs = [
                RetrievedDocument(
                    collection=collection.value,
                    doc_key=str(point.payload.get("doc_key")),
                    title=str(point.payload.get("title", "")),
                    text=str(point.payload.get("text", "")),
                    entity_name=point.payload.get("entity_name"),
                    score=score,
                    dense_rank=ranks[0],
                    sparse_rank=ranks[1],
                    untrusted_flagged=bool(point.payload.get("untrusted_flagged", False)),
                )
                for point, score, ranks in fused
            ]
            if self._reranker is not None and docs:
                rerank_scores = await self._reranker.rerank(
                    query, [f"{d.title}\n{d.text}" for d in docs]
                )
                docs = [
                    RetrievedDocument(**{**asdict(d), "score": s})
                    for d, s in sorted(
                        zip(docs, rerank_scores, strict=True),
                        key=lambda pair: (-pair[1], pair[0].doc_key),
                    )
                ]
            results.extend(docs[:top_k])

        await self._cache_set(key, results)
        RETRIEVAL_DURATION.labels(cache="miss").observe(time.perf_counter() - started)
        return results
