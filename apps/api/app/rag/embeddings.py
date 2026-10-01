"""Dense and sparse text encoders.

Dense (``EmbeddingProvider``):
* ``HashingEmbeddings`` - deterministic feature hashing of words and character
  n-grams into ``EMBEDDING_DIMENSIONS``; no model, no network. Development/tests
  only (rejected in production): it captures lexical, not semantic, similarity.
* ``OpenAICompatibleEmbeddings`` - ``POST {base}/embeddings`` (vLLM, TEI,
  llama.cpp, Ollama's ``/v1``).
* ``OllamaEmbeddings`` - ``POST {base}/api/embed``.

Sparse (``SparseEncoder``): hashed term frequencies for Qdrant sparse vectors with
the IDF modifier, i.e. BM25-like lexical matching of exact ERP terms and codes.
"""

from __future__ import annotations

import hashlib
import math
import struct
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any, Final, Protocol

import httpx

from app.config import EmbeddingProviderName, Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger
from app.core.metrics import EMBEDDING_DURATION
from app.core.resilience import CircuitBreaker, call_with_resilience
from app.rag.text import tokenize

logger = get_logger(__name__)

SPARSE_DIMENSION_BITS: Final = 24  # 16.7M buckets: collisions negligible for ERP vocabularies


class EmbeddingError(DependencyUnavailableError):
    code = "EMBEDDING_FAILED"
    default_message = "The embedding service returned an invalid response."


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm > 0 else vector


class HashingEmbeddings:
    name = "hashing"

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def _features(self, text: str) -> Counter[str]:
        tokens = tokenize(text)
        feats: Counter[str] = Counter(f"w:{t}" for t in tokens)
        for token in tokens:
            padded = f"#{token}#"
            for n in (3, 4):
                feats.update(
                    f"c{n}:{padded[i : i + n]}" for i in range(max(1, len(padded) - n + 1))
                )
        return feats

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, count in self._features(text).items():
            h = _hash(feature)
            sign = 1.0 if (h >> 63) & 1 else -1.0
            weight = (2.0 if feature.startswith("w:") else 1.0) * (1.0 + math.log(count))
            vector[h % self.dimensions] += sign * weight
        return _normalise(vector)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class _HttpEmbeddings:
    name: str

    def __init__(self, settings: Settings, http: httpx.AsyncClient, base_url: str) -> None:
        self.dimensions = settings.embedding_dimensions
        self._http = http
        self._base = base_url.rstrip("/")
        self._model = settings.embedding_model
        self._batch = settings.embedding_batch_size
        self._timeout = settings.embedding_timeout_seconds
        self._retries = settings.llm_max_retries
        key = settings.embedding_api_key or settings.llm_api_key
        self._headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
        self._breaker = CircuitBreaker(
            f"embeddings:{self.name}",
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_seconds=settings.llm_circuit_reset_seconds,
        )

    async def _request(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def _check(self, vectors: Any, expected: int) -> list[list[float]]:
        if not isinstance(vectors, list) or len(vectors) != expected:
            raise EmbeddingError(details={"reason": "COUNT_MISMATCH"})
        out: list[list[float]] = []
        for vec in vectors:
            if not isinstance(vec, list) or len(vec) != self.dimensions:
                raise EmbeddingError(
                    "Embedding dimensions do not match EMBEDDING_DIMENSIONS.",
                    details={"reason": "DIMENSION_MISMATCH", "expected": self.dimensions},
                )
            out.append(_normalise([float(v) for v in vec]))
        return out

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for start in range(0, len(texts), self._batch):
            batch = list(texts[start : start + self._batch])
            began = time.perf_counter()

            async def call(batch: list[str] = batch) -> list[list[float]]:
                return await self._request(batch)

            vectors = await call_with_resilience(
                call, breaker=self._breaker, max_retries=self._retries
            )
            EMBEDDING_DURATION.labels(provider=self.name).observe(time.perf_counter() - began)
            result.extend(self._check(vectors, len(batch)))
        return result

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class OpenAICompatibleEmbeddings(_HttpEmbeddings):
    name = "openai_compatible"

    async def _request(self, texts: list[str]) -> list[list[float]]:
        response = await self._http.post(
            f"{self._base}/embeddings",
            json={"model": self._model, "input": texts},
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list):
            raise EmbeddingError(details={"reason": "MALFORMED_RESPONSE"})
        ordered = sorted(data, key=lambda d: int(d.get("index", 0)))
        return [d.get("embedding") for d in ordered]


class OllamaEmbeddings(_HttpEmbeddings):
    name = "ollama"

    async def _request(self, texts: list[str]) -> list[list[float]]:
        response = await self._http.post(
            f"{self._base}/api/embed",
            json={"model": self._model, "input": texts},
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        vectors = response.json().get("embeddings")
        if not isinstance(vectors, list):
            raise EmbeddingError(details={"reason": "MALFORMED_RESPONSE"})
        return vectors


def build_embedding_provider(settings: Settings, http: httpx.AsyncClient) -> EmbeddingProvider:
    provider = settings.embedding_provider
    if provider is EmbeddingProviderName.HASHING:
        return HashingEmbeddings(settings.embedding_dimensions)
    base = settings.embedding_base_url or settings.llm_base_url
    if provider is EmbeddingProviderName.OLLAMA:
        return OllamaEmbeddings(settings, http, base)
    return OpenAICompatibleEmbeddings(settings, http, base)


# ------------------------------------------------------------------- sparse
class SparseEncoder:
    """Hashed term-frequency sparse vectors (IDF applied by Qdrant)."""

    mask: Final = (1 << SPARSE_DIMENSION_BITS) - 1

    def _index(self, token: str) -> int:
        return _hash(f"s:{token}") & self.mask

    def encode(self, text: str, *, query: bool = False) -> tuple[list[int], list[float]]:
        counts: Counter[int] = Counter(self._index(t) for t in tokenize(text))
        if not counts:
            return [], []
        indices = sorted(counts)
        if query:
            return indices, [1.0] * len(indices)
        return indices, [1.0 + math.log(counts[i]) for i in indices]


def vector_fingerprint(vector: Sequence[float]) -> str:
    """Stable short hash of a vector (used by tests and cache keys)."""
    return hashlib.sha256(struct.pack(f"{len(vector)}f", *vector)).hexdigest()[:16]
