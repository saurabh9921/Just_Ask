"""Background exports of governed query results (``/api/v1/exports``).

Request side (``ExportService``): the owner of a *succeeded* query with
``analytics.export`` asks for a CSV/JSON export. Detailed exports need explicit
confirmation (``confirm=true``); ``scope=all_rows`` removes the interactive row cap up
to ``EXPORT_MAX_ROWS``. At most ``EXPORT_MAX_ACTIVE_PER_USER`` exports run at once.

Job side (``run_export``, inline in dev or on the ``exports`` Celery queue):

1. re-load the user's principal (still active, still ``analytics.export``) and check
   the connection is still visible to them;
2. re-resolve the *stored, validated* plan against the current semantic version and
   catalog - authorization and tenant row filters are applied again, never trusted
   from the original request;
3. compile, run the independent SQL safety check, stream rows in batches from the
   read-only connection (server-side cursor; memory bounded by one batch);
4. write CSV/JSON to ``EXPORT_DIR/<tenant>/<random>.part``, scanning every cell for
   restricted values, then rename atomically and record size + SHA-256;
5. the file expires after ``EXPORT_TTL_SECONDS`` (``purge_expired_exports`` deletes it).

Downloads use a one-time token (``POST /exports/{id}/download-token``) redeemed at
``GET /exports/download/{token}`` within ``EXPORT_DOWNLOAD_TOKEN_TTL_SECONDS``; the
token is stored hashed in Redis and deleted on first use. Every step is audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.common.network import HostPolicy
from app.connectors.common.types import ObjectMetadata
from app.connectors.factory import AdapterCache
from app.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import get_logger
from app.core.metrics import EXPORT_JOBS, EXPORT_ROWS
from app.core.permissions import Permission
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import (
    DatabaseConnection,
    ExportJob,
    MetadataObject,
    QueryRecord,
    SemanticVersion,
    Tenant,
)
from app.query.plans import QueryPlan
from app.query.result_formatter import humanize
from app.query.sql_policy import build_sql_policy
from app.query.sql_validator import validate_sql
from app.query.validator import PlanContext, resolve_plan
from app.schemas.exports import DownloadTokenOut, ExportCreate, ExportOut, ExportPage
from app.semantic.registry import SemanticLayerRegistry
from app.semantic.validator import bind_to_catalog
from app.services.analytics_service import tenant_today
from app.services.audit_service import AuditOutcome, AuditService
from app.services.connection_service import ConnectionService
from app.services.export_writer import ExportWriter, SensitiveDataDetectedError
from app.services.secrets_service import SecretCipher
from app.workers.jobs import JobRunner

logger = get_logger(__name__)

ACTIVE = ("pending", "running")
DOWNLOAD_KEY_PREFIX = "export:dl:"
_SLUG = re.compile(r"[^a-z0-9]+")


class ExportConfirmationRequiredError(BadRequestError):
    code = "CONFIRMATION_REQUIRED"
    default_message = "Exports of detailed data must be confirmed (confirm=true)."


class TooManyExportsError(ConflictError):
    code = "TOO_MANY_EXPORTS"
    default_message = "Too many exports are running. Wait for one to finish."


class ExportNotReadyError(ConflictError):
    code = "EXPORT_NOT_READY"
    default_message = "The export is not ready for download."


class ExportExpiredError(AppError):
    status_code = 410
    code = "EXPORT_EXPIRED"
    default_message = "The export has expired. Request a new one."


class QueryNotExportableError(ConflictError):
    code = "QUERY_NOT_EXPORTABLE"
    default_message = "Only answered questions can be exported."


def to_out(job: ExportJob) -> ExportOut:
    return ExportOut(
        export_id=job.id,
        query_id=job.query_id,
        format=job.format,
        scope=job.scope,
        status=job.status,
        row_count=job.row_count,
        byte_size=job.byte_size,
        truncated=job.truncated,
        file_name=job.file_name,
        error_code=job.error_code,
        created_at=job.created_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
        downloaded_at=job.downloaded_at,
    )


def export_root(settings: Settings) -> Path:
    return Path(settings.export_dir).resolve()


def export_path(settings: Settings, job: ExportJob) -> Path:
    """Absolute file path; storage keys are generated by us, the check is defence in depth."""
    if not job.storage_key or not re.fullmatch(r"[0-9a-f]{32}\.(csv|json)", job.storage_key):
        raise NotFoundError()
    root = export_root(settings)
    path = (root / str(job.tenant_id) / job.storage_key).resolve()
    if not path.is_relative_to(root):
        raise NotFoundError()
    return path


def download_name(question: str, fmt: str, created: Any) -> str:
    slug = _SLUG.sub("-", question.lower()).strip("-")[:60] or "export"
    stamp = created.strftime("%Y%m%d-%H%M%S") if created is not None else "export"
    return f"erp-insight-{slug}-{stamp}.{fmt}"


def _token_key(token: str) -> str:
    return DOWNLOAD_KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ request side
@dataclass
class ExportService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    audit: AuditService
    redis: Redis
    jobs: JobRunner

    async def _own(self, export_id: uuid.UUID) -> ExportJob:
        job = await self.session.scalar(
            select(ExportJob).where(
                ExportJob.id == export_id,
                ExportJob.tenant_id == self.ctx.tenant_id,
                ExportJob.user_id == self.ctx.user_id,
            )
        )
        if job is None:
            raise NotFoundError()
        return job

    async def create(self, body: ExportCreate) -> ExportOut:
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        if not body.confirm:
            raise ExportConfirmationRequiredError()
        record = await self.session.scalar(
            select(QueryRecord).where(
                QueryRecord.id == body.query_id,
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.deleted_at.is_(None),
            )
        )
        if record is None:
            raise NotFoundError()
        if record.status != "succeeded" or not record.query_plan or record.connection_id is None:
            raise QueryNotExportableError()
        active = await self.session.scalar(
            select(func.count())
            .select_from(ExportJob)
            .where(
                ExportJob.tenant_id == self.ctx.tenant_id,
                ExportJob.user_id == self.ctx.user_id,
                ExportJob.status.in_(ACTIVE),
            )
        )
        if int(active or 0) >= self.settings.export_max_active_per_user:
            raise TooManyExportsError()
        job = ExportJob(
            tenant_id=self.ctx.tenant_id,
            user_id=self.ctx.user_id,
            query_id=record.id,
            format=body.format,
            scope=body.scope,
            status="pending",
            created_at=utcnow(),
        )
        self.session.add(job)
        await self.session.commit()
        await self.audit.record(
            "export.requested",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="export",
            resource_id=str(job.id),
            details={"query_id": str(record.id), "format": body.format, "scope": body.scope},
        )
        EXPORT_JOBS.labels("requested").inc()
        await self.jobs.submit(job.id)
        return to_out(job)

    async def list(self, limit: int = 20) -> ExportPage:
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        rows = await self.session.scalars(
            select(ExportJob)
            .where(ExportJob.tenant_id == self.ctx.tenant_id, ExportJob.user_id == self.ctx.user_id)
            .order_by(ExportJob.created_at.desc(), ExportJob.id)
            .limit(limit)
        )
        return ExportPage(items=[to_out(j) for j in rows])

    async def get(self, export_id: uuid.UUID) -> ExportOut:
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        return to_out(await self._own(export_id))

    async def delete(self, export_id: uuid.UUID) -> None:
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        job = await self._own(export_id)
        if job.status in ACTIVE:
            raise ConflictError("A running export cannot be deleted yet.")
        _remove_file(self.settings, job)
        job.status = "expired"
        job.storage_key = None
        job.expires_at = utcnow()
        await self.session.commit()
        await self.audit.record(
            "export.deleted",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="export",
            resource_id=str(export_id),
        )

    async def owned(self, export_id: uuid.UUID) -> ExportJob:
        """The caller's export (permission + ownership checked)."""
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        return await self._own(export_id)

    async def download_token(self, export_id: uuid.UUID) -> DownloadTokenOut:
        self.ctx.require(Permission.ANALYTICS_EXPORT)
        job = await self._own(export_id)
        ready_file(self.settings, job)
        token = secrets.token_urlsafe(32)
        payload = json.dumps(
            {"export_id": str(job.id), "tenant_id": str(job.tenant_id), "user_id": str(job.user_id)}
        )
        ttl = self.settings.export_download_token_ttl_seconds
        try:
            await self.redis.set(_token_key(token), payload, ex=ttl)
        except (RedisError, OSError) as exc:
            raise DependencyUnavailableError() from exc
        return DownloadTokenOut(url=f"/api/v1/exports/download/{token}", expires_in=ttl)


def ready_file(settings: Settings, job: ExportJob) -> Path:
    """Path of a downloadable export, or the reason it is not downloadable."""
    if job.status == "expired" or (
        job.expires_at is not None and _aware(job.expires_at) <= utcnow()
    ):
        raise ExportExpiredError()
    if job.status != "succeeded":
        raise ExportNotReadyError()
    path = export_path(settings, job)
    if not path.is_file():
        raise ExportExpiredError()
    return path


async def mark_downloaded(
    session: AsyncSession, audit: AuditService, job: ExportJob, via: str
) -> None:
    job.downloaded_at = utcnow()
    await session.commit()
    await audit.record(
        "export.downloaded",
        AuditOutcome.SUCCESS,
        tenant_id=job.tenant_id,
        actor_user_id=job.user_id,
        resource_type="export",
        resource_id=str(job.id),
        details={"via": via, "rows": job.row_count, "bytes": job.byte_size},
    )
    EXPORT_JOBS.labels("downloaded").inc()


async def redeem_download_token(session: AsyncSession, redis: Redis, token: str) -> ExportJob:
    """Consume a one-time token; returns the export it grants (404 when unknown/used)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        raise NotFoundError()
    try:
        raw = await redis.getdel(_token_key(token))
    except (RedisError, OSError) as exc:
        raise DependencyUnavailableError() from exc
    if raw is None:
        raise NotFoundError()
    data = json.loads(raw)
    job = await session.scalar(
        select(ExportJob).where(
            ExportJob.id == uuid.UUID(data["export_id"]),
            ExportJob.tenant_id == uuid.UUID(data["tenant_id"]),
            ExportJob.user_id == uuid.UUID(data["user_id"]),
        )
    )
    if job is None:
        raise NotFoundError()
    return job


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _remove_file(settings: Settings, job: ExportJob) -> None:
    if not job.storage_key:
        return
    try:
        export_path(settings, job).unlink(missing_ok=True)
    except (NotFoundError, OSError):
        logger.warning("export file could not be removed", extra={"export_id": str(job.id)})


# ------------------------------------------------------------------ job side
@dataclass(slots=True)
class ExportDeps:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    cipher: SecretCipher
    adapters: AdapterCache
    host_policy: HostPolicy
    audit: AuditService
    registry: SemanticLayerRegistry


class ExportFailedError(Exception):
    """Job-side failure with a stable error code (never shown with internal detail)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _prepare(
    deps: ExportDeps, session: AsyncSession, job: ExportJob
) -> tuple[TenantContext, QueryRecord, ConnectionService, DatabaseConnection]:
    """Re-authorize the export at run time; nothing is trusted from the request."""
    from app.dependencies import load_user_context  # noqa: PLC0415 - avoids an import cycle

    ctx = await load_user_context(session, user_id=job.user_id, tenant_id=job.tenant_id)
    if not ctx.has(Permission.ANALYTICS_EXPORT):
        raise PermissionDeniedError()
    record = await session.get(QueryRecord, job.query_id)
    if (
        record is None
        or record.tenant_id != job.tenant_id
        or record.deleted_at is not None
        or not record.query_plan
        or record.connection_id is None
    ):
        raise ExportFailedError("QUERY_NOT_EXPORTABLE")
    connections = ConnectionService(
        session=session,
        settings=deps.settings,
        ctx=ctx,
        cipher=deps.cipher,
        adapters=deps.adapters,
        host_policy=deps.host_policy,
        audit=deps.audit,
    )
    conn = await connections.get(record.connection_id)  # 404 if no longer visible
    return ctx, record, connections, conn


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, ExportFailedError | AppError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "EXPORT_TIMEOUT"
    return "EXPORT_FAILED"


async def run_export(deps: ExportDeps, export_id: uuid.UUID) -> None:
    """Produce one export file. Idempotent: only ``pending`` jobs are processed."""
    settings = deps.settings
    async with deps.session_factory() as session:
        job = await session.get(ExportJob, export_id)
        if job is None or job.status != "pending":
            return
        job.status = "running"
        job.started_at = utcnow()
        await session.commit()
        tmp: Path | None = None
        outcome = AuditOutcome.SUCCESS
        details: dict[str, Any]
        try:
            ctx, record, connections, conn = await _prepare(deps, session, job)
            async with asyncio.timeout(settings.export_timeout_seconds):
                tmp, summary, truncated = await _produce(
                    deps, session, ctx, connections, conn, record, job
                )
            final = tmp.with_suffix(f".{job.format}")
            tmp.replace(final)
            finished = utcnow()
            job.storage_key = final.name
            job.status = "succeeded"
            job.row_count = summary.row_count
            job.byte_size = summary.byte_size
            job.sha256 = summary.sha256
            job.truncated = truncated
            job.file_name = download_name(record.question, job.format, job.created_at)
            job.finished_at = finished
            job.expires_at = finished + timedelta(seconds=settings.export_ttl_seconds)
            await session.commit()
            EXPORT_JOBS.labels("succeeded").inc()
            EXPORT_ROWS.inc(summary.row_count)
            details = {"rows": summary.row_count, "bytes": summary.byte_size}
        except Exception as exc:  # noqa: BLE001 - every failure must end in a terminal state
            await session.rollback()
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            code = _failure_code(exc)
            logger.warning(
                "export failed",
                extra={
                    "export_id": str(export_id),
                    "error_code": code,
                    "error_type": type(exc).__name__,
                },
            )
            await _mark_failed(session, export_id, code)
            denied = isinstance(exc, SensitiveDataDetectedError | PermissionDeniedError)
            outcome = AuditOutcome.DENIED if denied else AuditOutcome.ERROR
            details = {"error_code": code}
    await deps.audit.record(
        "export.completed",
        outcome,
        tenant_id=job.tenant_id,
        actor_user_id=job.user_id,
        resource_type="export",
        resource_id=str(export_id),
        details=details,
    )


async def _mark_failed(session: AsyncSession, export_id: uuid.UUID, code: str) -> None:
    failed = await session.get(ExportJob, export_id)
    if failed is not None:
        failed.status = "failed"
        failed.error_code = code[:64]
        failed.finished_at = utcnow()
        failed.expires_at = failed.finished_at
        await session.commit()
    EXPORT_JOBS.labels("failed").inc()


async def _produce(
    deps: ExportDeps,
    session: AsyncSession,
    ctx: TenantContext,
    connections: ConnectionService,
    conn: DatabaseConnection,
    record: QueryRecord,
    job: ExportJob,
) -> tuple[Path, Any, bool]:
    settings = deps.settings
    active = await session.scalar(
        select(SemanticVersion).where(
            SemanticVersion.connection_id == conn.id,
            SemanticVersion.tenant_id == ctx.tenant_id,
            SemanticVersion.status == "active",
        )
    )
    if active is None or active.metadata_snapshot_id is None:
        raise ExportFailedError("SEMANTIC_INDEX_NOT_READY")
    rows = await session.scalars(
        select(MetadataObject).where(
            MetadataObject.snapshot_id == active.metadata_snapshot_id,
            MetadataObject.tenant_id == ctx.tenant_id,
        )
    )
    catalog = [ObjectMetadata.model_validate(r.definition) for r in rows]
    bundle = deps.registry.get()
    bindings = bind_to_catalog(bundle, catalog)
    tenant = await session.get(Tenant, ctx.tenant_id)
    adapter = await connections.adapter_for(conn)
    limit = settings.export_max_rows if job.scope == "all_rows" else None
    plan = QueryPlan.model_validate(record.query_plan)
    resolved = resolve_plan(
        plan,
        bundle=bundle,
        bindings=bindings,
        catalog=catalog,
        ctx=PlanContext(
            tenant_id=ctx.tenant_id,
            tenant_slug=ctx.tenant_slug,
            tenant_settings=dict(tenant.settings) if tenant else {},
            connection_id=conn.id,
            semantic_version=active.version,
            permissions=frozenset(p.value for p in ctx.permissions),
            today=tenant_today(dict(tenant.settings) if tenant else {}),
            max_rows=min(settings.max_query_rows, adapter.config.max_rows),
        ),
    )
    # "result": the answer's own query (its LIMIT is part of the question, e.g. top 5).
    # "all_rows": no interactive cap; one extra row is fetched to detect truncation at
    # EXPORT_MAX_ROWS.
    cap = limit if limit is not None else resolved.limit
    if limit is not None:
        resolved.limit = limit + 1
    compiled = adapter.compile_query_plan(resolved)
    policy = build_sql_policy(
        bundle,
        resolved,
        adapter.dialect.name,
        max_limit=max(resolved.limit, settings.max_query_rows) + 1,
    )
    statements = [("current", compiled.statement)]
    validate_sql(compiled.render(adapter.dialect), policy)
    if compiled.previous_statement is not None:
        validate_sql(compiled.render(adapter.dialect, previous=True), policy)
        statements.append(("previous", compiled.previous_statement))

    columns = [(c.name, humanize(c.name)) for c in compiled.output]
    if len(statements) > 1:
        columns = [("period", "Period"), *columns]
    directory = export_root(settings) / str(ctx.tenant_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{uuid.uuid4().hex}.part"
    truncated = False
    with ExportWriter(tmp, job.format, columns) as writer:  # type: ignore[arg-type]
        for period, statement in statements:
            written = 0
            async for batch in adapter.stream_readonly(
                statement,
                batch_size=settings.export_batch_size,
                max_rows=cap + 1,
                ceiling=settings.export_max_rows + 1,
                query_id=f"export-{job.id}",
            ):
                room = cap - written
                chunk = batch[:room]
                truncated = truncated or len(batch) > room
                if len(statements) > 1:
                    chunk = [(period, *row) for row in chunk]
                writer.write_batch(chunk)
                written += len(chunk)
        writer.finish(
            {
                "question": record.question,
                "query_id": str(record.id),
                "exported_at": utcnow().isoformat(),
                "truncated": truncated,
            }
        )
    return tmp, writer.summary(), truncated


async def purge_expired_exports(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """Delete expired export files and mark their jobs ``expired``. Idempotent."""
    now = utcnow()
    async with session_factory() as session:
        jobs = list(
            await session.scalars(
                select(ExportJob).where(
                    ExportJob.status.in_(("succeeded", "failed")), ExportJob.expires_at <= now
                )
            )
        )
        for job in jobs:
            _remove_file(settings, job)
        if jobs:
            await session.execute(
                update(ExportJob)
                .where(ExportJob.id.in_([j.id for j in jobs]))
                .values(status="expired", storage_key=None)
            )
            await session.commit()
    if jobs:
        EXPORT_JOBS.labels("expired").inc(len(jobs))
    return len(jobs)
