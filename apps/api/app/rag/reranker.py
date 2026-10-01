"""Optional second-stage reranking of fused candidates.

* ``LexicalReranker`` - deterministic token-overlap scoring (development/tests).
* ``HttpReranker`` - Jina/vLLM-style ``POST {base}/rerank``
  ``{"model", "query", "documents", "top_n"}`` → ``{"results": [{"index", "relevance_score"}]}``.
Reranking only reorders candidates that already passed the scoped retrieval
filter; it can never introduce new documents.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.config import RerankerProviderName, Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.resilience import CircuitBreaker, call_with_resilience
from app.rag.text import tokenize


class RerankError(DependencyUnavailableError):
    code = "RERANK_FAILED"
    default_message = "The reranking service returned an invalid response."


class Reranker(Protocol):
    name: str

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """Return one relevance score per document (higher is better)."""
        ...


class LexicalReranker:
    name = "lexical"

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        q = set(tokenize(query))
        if not q:
            return [0.0] * len(documents)
        scores = []
        for doc in documents:
            d = tokenize(doc)
            overlap = len(q & set(d))
            scores.append(overlap / len(q) + overlap / math.sqrt(len(d) + 1) / 10)
        return scores


class HttpReranker:
    name = "http"

    def __init__(self, settings: Settings, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + "/rerank"
        self._model = settings.reranker_model
        self._timeout = settings.embedding_timeout_seconds
        self._retries = settings.llm_max_retries
        key = settings.llm_api_key
        self._headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
        self._breaker = CircuitBreaker(
            "reranker",
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_seconds=settings.llm_circuit_reset_seconds,
        )

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []

        async def call() -> list[float]:
            response = await self._http.post(
                self._url,
                json={
                    "model": self._model,
                    "query": query,
                    "documents": list(documents),
                    "top_n": len(documents),
                },
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            results = response.json().get("results")
            if not isinstance(results, list):
                raise RerankError(details={"reason": "MALFORMED_RESPONSE"})
            scores = [float("-inf")] * len(documents)
            for item in results:
                index = int(item["index"])
                if not 0 <= index < len(documents):
                    raise RerankError(details={"reason": "INDEX_OUT_OF_RANGE"})
                scores[index] = float(item.get("relevance_score", item.get("score", 0.0)))
            return scores

        return await call_with_resilience(call, breaker=self._breaker, max_retries=self._retries)


def build_reranker(settings: Settings, http: httpx.AsyncClient) -> Reranker | None:
    if not settings.reranker_enabled:
        return None
    if settings.reranker_provider is RerankerProviderName.HTTP:
        return HttpReranker(settings, http, settings.reranker_base_url or settings.llm_base_url)
    return LexicalReranker()
