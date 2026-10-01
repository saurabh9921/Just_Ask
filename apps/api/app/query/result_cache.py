"""Query-result cache (Redis) and catalog cache (in-process).

Query results
    Key: ``qr:{tenant}:{connection}:{config}:v{semantic_version}:{scope}:{plan}:{sql}``

    * ``tenant`` / ``connection`` - never shared across tenants or data sources;
    * ``config`` - hash of the connection's configuration version (re-pointing a
      connection at another database invalidates everything);
    * ``semantic_version`` - a new semantic build invalidates results;
    * ``scope`` - hash of the caller's permissions and the tenant row-filter values
      (the authorization scope the result was computed under);
    * ``plan`` - the normalized plan hash, which includes the *resolved* date windows,
      so "this month" rolls over automatically; ``sql`` - the compiled statement hash.

    Values are the formatted result (already policy-redacted) with a timestamp; TTL is
    ``QUERY_CACHE_TTL_SECONDS``. Redis failures degrade to a cache miss.

Catalog
    Metadata snapshots are immutable, so the parsed catalog (and its bindings to a
    semantic bundle) is cached per ``(tenant, snapshot, bundle hash)`` in a small LRU.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.connectors.common.types import ObjectMetadata
from app.core.logging import get_logger
from app.core.metrics import CACHE_REQUESTS
from app.query.result_formatter import FormattedResult
from app.semantic.validator import EntityBinding

logger = get_logger(__name__)


def _digest(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def scope_hash(permissions: frozenset[str], row_filters: list[tuple[str, str, str]]) -> str:
    """Authorization scope: permissions + (source, field, value) tenant row filters."""
    material = json.dumps(
        {"permissions": sorted(permissions), "row_filters": sorted(row_filters)}, sort_keys=True
    )
    return _digest(material)


def result_cache_key(
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    config_version: str,
    semantic_version: int,
    scope: str,
    plan_hash: str,
    sql_hash: str,
) -> str:
    return (
        f"qr:{tenant_id}:{connection_id}:{_digest(config_version, 12)}:v{semantic_version}"
        f":{scope}:{plan_hash[:32]}:{sql_hash[:16]}"
    )


class CachedResult(BaseModel):
    result: FormattedResult
    stored_at: float
    execution_ms: float | None = None


@dataclass
class CacheHit:
    result: FormattedResult
    age_seconds: float


class QueryResultCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int, enabled: bool = True) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self.enabled = enabled and ttl_seconds > 0

    async def get(self, key: str) -> CacheHit | None:
        if not self.enabled:
            return None
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError):
            CACHE_REQUESTS.labels(cache="query_result", result="error").inc()
            return None
        if raw is None:
            CACHE_REQUESTS.labels(cache="query_result", result="miss").inc()
            return None
        try:
            cached = CachedResult.model_validate_json(raw)
        except ValidationError:
            CACHE_REQUESTS.labels(cache="query_result", result="error").inc()
            return None
        CACHE_REQUESTS.labels(cache="query_result", result="hit").inc()
        return CacheHit(cached.result, max(0.0, time.time() - cached.stored_at))

    async def set(self, key: str, result: FormattedResult, execution_ms: float | None) -> None:
        if not self.enabled:
            return
        payload = CachedResult(result=result, stored_at=time.time(), execution_ms=execution_ms)
        try:
            await self._redis.set(key, payload.model_dump_json(), ex=self._ttl)
        except (RedisError, OSError):
            logger.warning("query result cache write failed")


@dataclass(frozen=True)
class CatalogEntry:
    catalog: list[ObjectMetadata]
    row_estimates: dict[str, int]


class CatalogCache:
    """LRU of parsed catalogs and bindings; entries never go stale (snapshots are immutable)."""

    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._catalogs: OrderedDict[tuple[uuid.UUID, uuid.UUID], CatalogEntry] = OrderedDict()
        self._bindings: OrderedDict[tuple[uuid.UUID, uuid.UUID, str], dict[str, EntityBinding]] = (
            OrderedDict()
        )

    def _touch(self, store: OrderedDict[Any, Any], key: Any) -> Any:
        value = store.get(key)
        if value is not None:
            store.move_to_end(key)
        return value

    def _put(self, store: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        if self._max <= 0:
            return
        store[key] = value
        store.move_to_end(key)
        while len(store) > self._max:
            store.popitem(last=False)

    def get_catalog(self, tenant_id: uuid.UUID, snapshot_id: uuid.UUID) -> CatalogEntry | None:
        entry: CatalogEntry | None = self._touch(self._catalogs, (tenant_id, snapshot_id))
        CACHE_REQUESTS.labels(cache="catalog", result="hit" if entry else "miss").inc()
        return entry

    def put_catalog(
        self, tenant_id: uuid.UUID, snapshot_id: uuid.UUID, entry: CatalogEntry
    ) -> None:
        self._put(self._catalogs, (tenant_id, snapshot_id), entry)

    def get_bindings(
        self, tenant_id: uuid.UUID, snapshot_id: uuid.UUID, bundle_hash: str
    ) -> dict[str, EntityBinding] | None:
        value: dict[str, EntityBinding] | None = self._touch(
            self._bindings, (tenant_id, snapshot_id, bundle_hash)
        )
        return value

    def put_bindings(
        self,
        tenant_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        bundle_hash: str,
        bindings: dict[str, EntityBinding],
    ) -> None:
        self._put(self._bindings, (tenant_id, snapshot_id, bundle_hash), bindings)

    def __len__(self) -> int:
        return len(self._catalogs)
