"""Analytics queries: run the agent, persist history, idempotency and cancellation.

Lifecycle of one question::

    start()  -> QueryRecord(status="running") committed (visible to GET / cancel)
    run()    -> AnalyticsOrchestrator FSM (validate -> compile -> check -> execute)
             -> QueryRecord updated with hashes/counts (never rows or SQL values)
             -> response cached in Redis for ``query_result_ttl_seconds`` (GET /query/{id})

Cancellation works across API processes: ``cancel()`` sets a Redis flag and, if
the run lives in this process, trips its event and interrupts the database
query directly. A watcher polls the flag for runs owned by this process.

Idempotency: a repeated ``Idempotency-Key`` (per tenant and user, within
``idempotency_ttl_seconds``) replays the earlier outcome instead of running the
query again; reusing a key for a different question is a conflict.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import (
    AgentOutcome,
    AgentRequest,
    AnalyticsOrchestrator,
    Emit,
    OrchestratorDeps,
)
from app.agents.policies import AgentPolicy
from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.metrics import ANALYTICS_QUERIES, ANALYTICS_QUERY_DURATION
from app.core.permissions import Permission
from app.core.rate_limit import RateLimiter
from app.core.security import utcnow as security_utcnow
from app.core.tenant_context import TenantContext
from app.db.models import QueryRecord, Tenant
from app.llm.base import LLMProvider
from app.query.normalizer import Window
from app.query.result_cache import CatalogCache, QueryResultCache
from app.rag.hybrid_retriever import HybridRetriever
from app.schemas.analytics import (
    AppliedFilterOut,
    AppliedFiltersOut,
    CacheInfoOut,
    CancelResponse,
    ConnectionRef,
    CostOut,
    DateWindowOut,
    ExplanationOut,
    QueryErrorOut,
    QueryRecordOut,
    QueryResponse,
    SecurityScopeOut,
    TimingsOut,
    UsageOut,
    ValidationOut,
)
from app.semantic.registry import SemanticLayerRegistry
from app.services.audit_service import AuditOutcome, AuditService
from app.services.connection_service import ConnectionService

logger = get_logger(__name__)

Mode = Literal["execute", "plan"]
ACTIVE_STATUSES = frozenset({"pending", "running"})
IDEMPOTENCY_KEY_MAX = 128


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_KEY_REUSED"
    default_message = "This Idempotency-Key was already used for a different request."


class QueryInProgressError(ConflictError):
    code = "QUERY_IN_PROGRESS"
    default_message = "A request with this Idempotency-Key is still running."


def response_status(response: QueryResponse) -> int:
    """HTTP status for a terminal response (failures use the failing step's status)."""
    if response.status == "cancelled":
        return 409
    if response.status == "failed":
        return response.error.status_code if response.error else 500
    return 200


def validate_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX or not key.isprintable():
        raise BadRequestError("Idempotency-Key must be 1-128 printable characters.")
    return key


# ------------------------------------------------------------------ run registry
@dataclass
class ActiveRun:
    query_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    adapter: DatabaseAdapter | None = None


class QueryRunRegistry:
    """Runs executing in this process (for cancellation). Keyed by query ID."""

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, ActiveRun] = {}

    def add(self, run: ActiveRun) -> None:
        self._runs[run.query_id] = run

    def get(self, query_id: uuid.UUID) -> ActiveRun | None:
        return self._runs.get(query_id)

    def remove(self, query_id: uuid.UUID) -> None:
        self._runs.pop(query_id, None)

    def __len__(self) -> int:
        return len(self._runs)


def _cancel_key(query_id: uuid.UUID) -> str:
    return f"analytics:cancel:{query_id}"


def result_key(tenant_id: uuid.UUID, user_id: uuid.UUID, query_id: uuid.UUID) -> str:
    return f"analytics:result:{tenant_id}:{user_id}:{query_id}"


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def tenant_today(settings: dict[str, Any], now: datetime | None = None) -> date:
    """'Today' in the tenant's configured time zone (``settings.timezone``), UTC otherwise."""
    now = now or utcnow()
    name = settings.get("timezone")
    if isinstance(name, str) and name:
        try:
            return now.astimezone(ZoneInfo(name)).date()
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown tenant time zone; using UTC")
    return now.date()


def _window(field_ref: str, window: Window | None) -> DateWindowOut | None:
    if window is None:
        return None
    return DateWindowOut(
        field=field_ref, start=window.start, end_exclusive=window.end, label=window.label()
    )


def build_response(
    out: AgentOutcome,
    *,
    question: str,
    connection_id: uuid.UUID,
    tenant_slug: str,
) -> QueryResponse:
    resolved = out.resolved
    applied = AppliedFiltersOut()
    if resolved is not None:
        tf = resolved.time_filter
        applied = AppliedFiltersOut(
            date_window=_window(tf.ref, tf.window) if tf else None,
            previous_window=_window(tf.ref, tf.previous) if tf else None,
            filters=[
                AppliedFilterOut(field=f.ref, op=f.op.value, value=f.value)
                for f in resolved.filters
            ],
        )
    explanation = None
    if out.explanation is not None and out.explanation_source is not None:
        explanation = ExplanationOut(
            summary=out.explanation.summary,
            highlights=list(out.explanation.highlights),
            source=out.explanation_source,
        )
    error = None
    if out.error is not None:
        error = QueryErrorOut(
            code=out.error.code,
            message=out.error.message,
            status_code=out.error.status_code,
            details=out.error.details,
        )
    return QueryResponse(
        query_id=out.query_id,
        status=out.status,
        question=question,
        intent=out.intent.intent if out.intent else None,
        connection=ConnectionRef(
            id=connection_id, name=out.connection_name, connector_type=out.connector_type
        ),
        semantic_version=out.semantic_version,
        data_freshness=out.data_freshness,
        plan=out.plan.model_dump(mode="json", exclude_defaults=True) if out.plan else None,
        description=out.description,
        assumptions=out.assumptions,
        source_entities=sorted(resolved.source_entities) if resolved else [],
        applied_filters=applied,
        security_scope=SecurityScopeOut(
            tenant=tenant_slug, row_filter_fields=out.row_filter_fields
        ),
        validation=ValidationOut(
            plan_valid=resolved is not None,
            sql_validated=out.sql_hash is not None,
            corrections=out.corrections,
            confidence=out.plan.confidence if out.plan else None,
        ),
        sql=out.sql,
        previous_sql=out.previous_sql,
        cost=CostOut(**out.cost.model_dump()) if out.cost else None,
        result=out.result,
        rows_returned=out.result.row_count if out.result else None,
        explanation=explanation,
        clarification_question=out.clarification_question,
        refusal_reason=out.refusal_reason,
        error=error,
        timings=TimingsOut(
            total_ms=out.timings.get("total_ms"),
            execution_ms=out.timings.get("execution_ms"),
            llm_ms=out.timings.get("llm_ms"),
        ),
        steps=out.steps,
        cache=CacheInfoOut(hit=out.cache_hit, age_seconds=out.cache_age_seconds),
        usage=UsageOut(
            llm_calls=out.llm_usage.calls,
            prompt_tokens=out.llm_usage.prompt_tokens,
            completion_tokens=out.llm_usage.completion_tokens,
        ),
        analysis=out.analysis,
    )


# ------------------------------------------------------------------ service
@dataclass
class AnalyticsService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    connections: ConnectionService
    registry: SemanticLayerRegistry
    retriever: HybridRetriever
    llm: LLMProvider
    audit: AuditService
    redis: Redis
    runs: QueryRunRegistry
    rate_limiter: RateLimiter | None = None
    result_cache: QueryResultCache | None = None
    catalog_cache: CatalogCache | None = None

    # -------------------------------------------------------------- lookups
    async def _record(self, query_id: uuid.UUID) -> QueryRecord:
        record = await self.session.scalar(
            select(QueryRecord).where(
                QueryRecord.id == query_id,
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.deleted_at.is_(None),
            )
        )
        if record is None:
            raise NotFoundError()
        return record

    async def previous_plan(self, parent_query_id: uuid.UUID) -> tuple[dict[str, Any], uuid.UUID]:
        """Plan and connection of an earlier query of this user (follow-up context)."""
        parent = await self._record(parent_query_id)
        if parent.query_plan is None or parent.connection_id is None:
            raise BadRequestError("The earlier query has no plan to follow up on.")
        return dict(parent.query_plan), parent.connection_id

    async def _replay(
        self, key: str, question: str, connection_id: uuid.UUID
    ) -> QueryResponse | None:
        since = datetime.now(tz=UTC) - timedelta(seconds=self.settings.idempotency_ttl_seconds)
        existing = await self.session.scalar(
            select(QueryRecord).where(
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.idempotency_key == key,
            )
        )
        if existing is None:
            return None
        created = existing.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < since:
            # Expired: free the key for this new request.
            existing.idempotency_key = None
            await self.session.commit()
            return None
        if existing.question != question or existing.connection_id != connection_id:
            raise IdempotencyConflictError()
        if existing.status in ACTIVE_STATUSES:
            raise QueryInProgressError(details={"query_id": str(existing.id)})
        cached = await self._cached_response(existing.id)
        if cached is not None:
            return cached.model_copy(update={"replayed": True})
        return self._response_from_record(existing).model_copy(update={"replayed": True})

    def _response_from_record(self, record: QueryRecord) -> QueryResponse:
        """Minimal response when the cached one has expired (rows are never persisted)."""
        status = record.status if record.status not in ACTIVE_STATUSES else "failed"
        error = None
        if record.error_code:
            error = QueryErrorOut(
                code=record.error_code,
                message="The query did not complete.",
                status_code=409 if record.status == "cancelled" else 500,
            )
        return QueryResponse(
            query_id=record.id,
            status=status,  # type: ignore[arg-type]
            question=record.question,
            intent=record.intent,
            connection=ConnectionRef(id=record.connection_id or uuid.UUID(int=0)),
            semantic_version=record.semantic_version,
            plan=record.query_plan,
            security_scope=SecurityScopeOut(tenant=self.ctx.tenant_slug),
            validation=ValidationOut(
                plan_valid=record.normalized_plan_hash is not None,
                sql_validated=record.compiled_sql_hash is not None,
            ),
            rows_returned=record.row_count,
            error=error,
            timings=TimingsOut(total_ms=record.total_ms, execution_ms=record.execution_ms),
        )

    async def _cached_response(self, query_id: uuid.UUID) -> QueryResponse | None:
        try:
            raw = await self.redis.get(result_key(self.ctx.tenant_id, self.ctx.user_id, query_id))
        except (RedisError, OSError):
            logger.warning("result cache unavailable")
            return None
        if raw is None:
            return None
        return QueryResponse.model_validate_json(raw)

    async def _cache_response(self, response: QueryResponse) -> None:
        ttl = self.settings.query_result_ttl_seconds
        if ttl <= 0:
            return
        try:
            await self.redis.set(
                result_key(self.ctx.tenant_id, self.ctx.user_id, response.query_id),
                response.model_dump_json(),
                ex=ttl,
            )
        except (RedisError, OSError):
            logger.warning("result cache unavailable")

    # -------------------------------------------------------------- running
    async def start(
        self,
        *,
        question: str,
        connection_id: uuid.UUID,
        idempotency_key: str | None,
        conversation_id: uuid.UUID | None = None,
        parent_query_id: uuid.UUID | None = None,
    ) -> QueryRecord | QueryResponse:
        """Authorize and register a run; returns a replayed response for a known key.

        The question joins ``conversation_id``, else its parent's conversation (a
        follow-up), else starts a new one.
        """
        self.ctx.require(Permission.ANALYTICS_QUERY)
        if self.rate_limiter is not None:
            await self.rate_limiter.enforce(
                "analytics_query",
                str(self.ctx.user_id),
                limit=self.settings.query_rate_limit_per_user_per_minute,
            )
            # Tenant-wide ceiling: one tenant cannot monopolise shared databases/models.
            await self.rate_limiter.enforce(
                "analytics_query_tenant",
                str(self.ctx.tenant_id),
                limit=self.settings.query_rate_limit_per_tenant_per_minute,
            )
        await self.connections.get(connection_id)  # 404 unless visible to this user
        if idempotency_key is not None:
            replay = await self._replay(idempotency_key, question, connection_id)
            if replay is not None:
                return replay
        if conversation_id is None and parent_query_id is not None:
            parent = await self._record(parent_query_id)
            conversation_id = parent.conversation_id or parent.id
        record_id = uuid.uuid4()
        record = QueryRecord(
            id=record_id,
            tenant_id=self.ctx.tenant_id,
            user_id=self.ctx.user_id,
            connection_id=connection_id,
            conversation_id=conversation_id or record_id,
            idempotency_key=idempotency_key,
            question=question,
            status="running",
            created_at=security_utcnow(),  # sub-second ordering on every database
        )
        self.session.add(record)
        try:
            await self.session.commit()
        except IntegrityError as exc:  # concurrent request with the same key
            await self.session.rollback()
            raise QueryInProgressError() from exc
        return record

    async def run(
        self,
        record: QueryRecord,
        *,
        mode: Mode = "execute",
        previous_plan: dict[str, Any] | None = None,
        emit: Emit | None = None,
    ) -> QueryResponse:
        assert record.connection_id is not None  # noqa: S101 - set by start()
        active = ActiveRun(
            query_id=record.id, tenant_id=self.ctx.tenant_id, user_id=self.ctx.user_id
        )
        self.runs.add(active)
        watcher = asyncio.create_task(self._watch_cancel(active))
        tenant = await self.session.get(Tenant, self.ctx.tenant_id)
        deps = OrchestratorDeps(
            settings=self.settings,
            session=self.session,
            ctx=self.ctx,
            connections=self.connections,
            registry=self.registry,
            retriever=self.retriever,
            llm=self.llm,
            audit=self.audit,
            today=tenant_today(dict(tenant.settings) if tenant else {}),
            result_cache=self.result_cache,
            catalog_cache=self.catalog_cache,
        )

        def adapter_ready(adapter: DatabaseAdapter) -> None:
            active.adapter = adapter

        request = AgentRequest(
            query_id=record.id,
            question=record.question,
            connection_id=record.connection_id,
            mode=mode,
            previous_plan=previous_plan,
            cancel=active.cancel,
            adapter_ready=adapter_ready,
        )
        try:
            outcome = await AnalyticsOrchestrator(
                deps, AgentPolicy.from_settings(self.settings), emit
            ).run(request)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            self.runs.remove(record.id)
        response = build_response(
            outcome,
            question=record.question,
            connection_id=record.connection_id,
            tenant_slug=self.ctx.tenant_slug,
        )
        await self._finish(record, outcome)
        await self._cache_response(response)
        ANALYTICS_QUERIES.labels(mode, outcome.status).inc()
        ANALYTICS_QUERY_DURATION.labels(mode, outcome.status).observe(
            outcome.timings.get("total_ms", 0.0) / 1000
        )
        return response

    async def _finish(self, record: QueryRecord, out: AgentOutcome) -> None:
        # The agent only read through this session; a cancelled or failed step may have
        # left its transaction invalid, so start clean before persisting the outcome.
        await self.session.rollback()
        record.status = out.status
        record.intent = out.intent.intent if out.intent else None
        record.query_plan = (
            out.plan.model_dump(mode="json", exclude_defaults=True) if out.plan else None
        )
        record.normalized_plan_hash = out.resolved.plan_hash if out.resolved else None
        record.compiled_sql_hash = out.sql_hash
        record.semantic_version = out.semantic_version
        record.row_count = out.result.row_count if out.result else None
        execution_ms = out.timings.get("execution_ms")
        record.execution_ms = round(execution_ms) if execution_ms is not None else None
        record.total_ms = round(out.timings.get("total_ms", 0.0))
        record.cache_hit = out.cache_hit
        record.error_code = out.error.code if out.error else None
        record.completed_at = datetime.now(tz=UTC)
        await self.session.commit()

    async def _watch_cancel(self, run: ActiveRun) -> None:
        """Mirror a cross-process cancel flag (Redis) into this run's event."""
        interval = self.settings.query_cancel_poll_seconds
        while not run.cancel.is_set():
            await asyncio.sleep(interval)
            try:
                flagged = await self.redis.exists(_cancel_key(run.query_id))
            except (RedisError, OSError):
                continue
            if flagged:
                await _trip(run)
                return

    async def cancel(self, query_id: uuid.UUID) -> CancelResponse:
        self.ctx.require(Permission.ANALYTICS_QUERY)
        record = await self._record(query_id)
        # End this read transaction before tripping the run: the run commits its final
        # status from its own session, and a reader held open here blocks that commit
        # (SQLite deadlocks it against the audit write below; PostgreSQL keeps an idle
        # transaction for nothing).
        await self.session.commit()
        if record.status not in ACTIVE_STATUSES:
            return CancelResponse(query_id=record.id, status=record.status, cancel_requested=False)
        try:
            ttl = max(60, self.settings.max_query_seconds * 4)
            await self.redis.set(_cancel_key(query_id), "1", ex=ttl)
        except (RedisError, OSError):
            logger.warning("cancel flag could not be stored; cancelling locally only")
        run = self.runs.get(query_id)
        if run is not None and run.tenant_id == self.ctx.tenant_id:
            await _trip(run)
        await self.audit.record(
            "analytics.cancel",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="query",
            resource_id=str(query_id),
        )
        return CancelResponse(query_id=record.id, status="cancelling", cancel_requested=True)

    async def get(self, query_id: uuid.UUID) -> QueryRecordOut:
        self.ctx.require(Permission.ANALYTICS_QUERY)
        record = await self._record(query_id)
        cached = await self._cached_response(query_id)
        done = record.status not in ACTIVE_STATUSES
        return QueryRecordOut(
            query_id=record.id,
            status=record.status,
            question=record.question,
            connection_id=record.connection_id,
            intent=record.intent,
            semantic_version=record.semantic_version,
            row_count=record.row_count,
            execution_ms=record.execution_ms,
            total_ms=record.total_ms,
            error_code=record.error_code,
            created_at=record.created_at,
            completed_at=record.completed_at,
            response=cached,
            response_expired=done and cached is None,
        )


async def _trip(run: ActiveRun) -> None:
    run.cancel.set()
    if run.adapter is not None:
        try:
            await run.adapter.cancel_query(str(run.query_id))
        except Exception:  # noqa: BLE001 - best effort; the event already stops the agent
            logger.warning("database-side cancel failed", extra={"query_id": str(run.query_id)})
