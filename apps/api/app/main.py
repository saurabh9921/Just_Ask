"""FastAPI application factory.

Run locally:  ``uvicorn app.main:app --reload``
Production:   ``uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.router import api_v1_router, ops_router
from app.config import Settings, get_settings
from app.connectors.common.network import HostPolicy
from app.connectors.factory import AdapterCache, ConnectorFactory
from app.core.exceptions import ErrorResponse, register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.metrics import APP_INFO
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordService, TokenService
from app.core.tracing import configure_tracing
from app.db.session import Database
from app.dependencies import AppResources
from app.llm.factory import build_llm_provider
from app.query.result_cache import CatalogCache, QueryResultCache
from app.rag.embeddings import SparseEncoder, build_embedding_provider
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.ingestion import IngestionDeps, run_ingestion
from app.rag.qdrant_client import VectorStore, build_qdrant_client
from app.rag.reranker import build_reranker
from app.semantic.registry import SemanticLayerRegistry
from app.services.analytics_service import QueryRunRegistry
from app.services.audit_service import AuditService
from app.services.export_service import ExportDeps, run_export
from app.services.health_service import (
    HealthCheck,
    HealthService,
    PostgresHealthCheck,
    QdrantHealthCheck,
    RedisHealthCheck,
)
from app.services.metadata_service import MetadataSyncDeps, run_snapshot
from app.services.secrets_service import SecretCipher
from app.workers.jobs import (
    ExportPayload,
    JobRunner,
    MetadataSyncPayload,
    SemanticIngestPayload,
)
from app.workers.queues import TaskName
from app.workers.tasks import TaskDispatcher, build_celery_client

logger = get_logger(__name__)


def build_resources(
    settings: Settings,
    *,
    redis: Redis | None = None,
    health_checks: list[HealthCheck] | None = None,
) -> AppResources:
    """Create every long-lived dependency. ``redis``/``health_checks`` are injectable for tests."""
    database = Database(settings)
    redis = redis or Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.readiness_check_timeout_seconds,
        socket_timeout=5,
        health_check_interval=30,
        decode_responses=True,
    )
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=settings.readiness_check_timeout_seconds),
        follow_redirects=False,
    )
    qdrant_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    qdrant = build_qdrant_client(settings.qdrant_url, qdrant_key)
    checks: list[HealthCheck] = (
        health_checks
        if health_checks is not None
        else [
            PostgresHealthCheck(database.engine),
            RedisHealthCheck(redis),
            QdrantHealthCheck(qdrant),
        ]
    )
    health = HealthService(
        checks,
        required=set(settings.readiness_required_checks),
        timeout_seconds=settings.readiness_check_timeout_seconds,
    )
    cipher = SecretCipher(settings)
    adapters = AdapterCache(ConnectorFactory(settings))
    host_policy = HostPolicy(settings.connection_host_denylist)
    audit = AuditService(database.session_factory)
    sync_deps = MetadataSyncDeps(
        settings=settings,
        session_factory=database.session_factory,
        cipher=cipher,
        adapters=adapters,
        host_policy=host_policy,
        audit=audit,
    )

    async def run_inline(snapshot_id: uuid.UUID) -> None:
        await run_snapshot(sync_deps, snapshot_id)

    dispatcher = (
        TaskDispatcher(build_celery_client(settings))
        if settings.metadata_sync_mode == "celery"
        else None
    )
    jobs = JobRunner(
        mode=settings.metadata_sync_mode,
        task=TaskName.METADATA_SYNC,
        payload=lambda job_id: MetadataSyncPayload(snapshot_id=job_id),
        inline=run_inline,
        dispatcher=dispatcher,
    )

    embedder = build_embedding_provider(settings, http)
    store = VectorStore(
        qdrant, prefix=settings.qdrant_collection_prefix, dimensions=embedder.dimensions
    )
    sparse = SparseEncoder()
    retriever = HybridRetriever(
        store=store,
        embedder=embedder,
        sparse=sparse,
        reranker=build_reranker(settings, http),
        redis=redis,
        candidate_k=settings.rag_candidate_k,
        rrf_k=settings.rag_rrf_k,
        cache_ttl_seconds=settings.retrieval_cache_ttl_seconds,
        max_query_chars=settings.rag_max_query_chars,
    )
    ingestion_deps = IngestionDeps(
        settings=settings,
        session_factory=database.session_factory,
        store=store,
        embedder=embedder,
        sparse=sparse,
        audit=audit,
    )

    async def run_ingest_inline(version_id: uuid.UUID) -> None:
        await run_ingestion(ingestion_deps, version_id)

    semantic_registry = SemanticLayerRegistry(settings.semantic_layer_path)
    export_deps = ExportDeps(
        settings=settings,
        session_factory=database.session_factory,
        cipher=cipher,
        adapters=adapters,
        host_policy=host_policy,
        audit=audit,
        registry=semantic_registry,
    )

    async def run_export_inline(export_id: uuid.UUID) -> None:
        await run_export(export_deps, export_id)

    export_jobs = JobRunner(
        mode=settings.metadata_sync_mode,
        task=TaskName.EXPORT_RUN,
        payload=lambda job_id: ExportPayload(export_id=job_id),
        inline=run_export_inline,
        dispatcher=dispatcher,
    )
    semantic_jobs = JobRunner(
        mode=settings.metadata_sync_mode,
        task=TaskName.SEMANTIC_INGEST,
        payload=lambda job_id: SemanticIngestPayload(version_id=job_id),
        inline=run_ingest_inline,
        dispatcher=dispatcher,
    )
    return AppResources(
        settings=settings,
        database=database,
        redis=redis,
        http=http,
        health=health,
        passwords=PasswordService(settings),
        tokens=TokenService(settings),
        rate_limiter=RateLimiter(redis),
        cipher=cipher,
        adapters=adapters,
        host_policy=host_policy,
        audit=audit,
        metadata_jobs=jobs,
        qdrant=qdrant,
        vector_store=store,
        retriever=retriever,
        llm=build_llm_provider(settings, http),
        semantic_registry=semantic_registry,
        semantic_jobs=semantic_jobs,
        analytics_runs=QueryRunRegistry(),
        ingestion=ingestion_deps,
        result_cache=QueryResultCache(
            redis,
            ttl_seconds=settings.query_cache_ttl_seconds,
            enabled=settings.query_result_cache_enabled,
        ),
        catalog_cache=CatalogCache(settings.catalog_cache_size),
        export_jobs=export_jobs,
    )


async def close_resources(resources: AppResources) -> None:
    await resources.metadata_jobs.close()
    await resources.semantic_jobs.close()
    await resources.export_jobs.close()
    await resources.qdrant.close()
    await resources.adapters.close_all()
    await resources.http.aclose()
    await resources.redis.aclose()
    await resources.database.dispose()


def create_app(
    settings: Settings | None = None, *, resources: AppResources | None = None
) -> FastAPI:
    """Build the application.

    ``resources`` may be injected (tests); otherwise they are created in the
    lifespan and closed on shutdown.
    """
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service=settings.app_name
    )
    configure_tracing(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = resources is None
        app.state.resources = resources or build_resources(settings)
        APP_INFO.labels(version=settings.app_version, environment=settings.app_env.value).set(1)
        logger.info("api started", extra={"environment": settings.app_env.value})
        try:
            yield
        finally:
            if owned:
                await close_resources(app.state.resources)
            logger.info("api stopped")

    docs_enabled = not settings.is_production
    app = FastAPI(
        title="ERP Insight AI API",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        responses={
            422: {"model": ErrorResponse, "description": "Validation error"},
            500: {"model": ErrorResponse, "description": "Internal error"},
        },
    )
    if resources is not None:
        app.state.resources = resources

    register_exception_handlers(app)

    # Middleware order: last added = outermost. The request-context middleware is
    # outermost so every response (including CORS rejections) gets an ID + headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)

    app.include_router(ops_router)
    app.include_router(api_v1_router)
    return app


def _create_default_app() -> FastAPI:
    return create_app()


app = _create_default_app()
