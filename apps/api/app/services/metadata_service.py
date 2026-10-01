"""Metadata discovery: versioned structure snapshots of ERP databases.

Flow: ``request_sync`` creates a ``pending`` snapshot (or returns the one already
in flight) and submits a job; ``run_snapshot`` (worker or inline task) reflects the
catalog through the connection's read-only adapter, fingerprints every object,
diffs against the previous successful snapshot and stores the new version.

Only structure is stored (names, types, keys, comments, row *estimates*).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.common.network import HostPolicy
from app.connectors.common.types import ObjectMetadata
from app.connectors.factory import AdapterCache
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.metrics import METADATA_SYNC_DURATION
from app.core.permissions import Permission
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import DatabaseConnection, MetadataObject, MetadataSnapshot
from app.schemas.metadata import (
    ConnectionSyncStatus,
    EntityDetail,
    EntityList,
    EntitySummary,
    SyncStatusOut,
)
from app.services.audit_service import AuditOutcome, AuditService
from app.services.connection_service import ConnectionService, adapter_for_connection
from app.services.secrets_service import SecretCipher
from app.workers.jobs import is_abandoned

logger = get_logger(__name__)

MAX_METADATA_OBJECTS: Final = 50_000
ACTIVE_STATUSES: Final = ("pending", "running")


def fingerprint(obj: ObjectMetadata) -> str:
    """Deterministic hash of an object's structure (row estimates excluded - they drift)."""
    payload = obj.model_dump(mode="json", exclude={"row_estimate"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def to_status(snapshot: MetadataSnapshot) -> SyncStatusOut:
    return SyncStatusOut(
        snapshot_id=snapshot.id,
        version=snapshot.version,
        status=snapshot.status,
        object_count=snapshot.object_count,
        added_count=snapshot.added_count,
        changed_count=snapshot.changed_count,
        removed_count=snapshot.removed_count,
        error_code=snapshot.error_code,
        server_version=snapshot.server_version,
        created_at=snapshot.created_at,
        started_at=snapshot.started_at,
        finished_at=snapshot.finished_at,
    )


@dataclass(slots=True)
class MetadataService:
    session: AsyncSession
    ctx: TenantContext
    connections: ConnectionService
    audit: AuditService

    async def request_sync(self, connection_id: uuid.UUID) -> tuple[MetadataSnapshot, bool]:
        """Return (snapshot, created). An in-flight sync is returned instead of a new one."""
        self.ctx.require(Permission.CONNECTIONS_MANAGE)
        conn = await self.connections.get(connection_id)
        active = await self.session.scalar(
            select(MetadataSnapshot)
            .where(
                MetadataSnapshot.connection_id == conn.id,
                MetadataSnapshot.status.in_(ACTIVE_STATUSES),
            )
            .order_by(MetadataSnapshot.version.desc())
            .limit(1)
        )
        if active is not None:
            if not is_abandoned(active.created_at, active.started_at):
                return active, False
            active.status = "failed"
            active.error_code = "ABANDONED"
            active.finished_at = utcnow()
            logger.warning(
                "abandoned metadata sync superseded",
                extra={"snapshot_id": str(active.id), "connection_id": str(conn.id)},
            )
        current = await self.session.scalar(
            select(func.max(MetadataSnapshot.version)).where(
                MetadataSnapshot.connection_id == conn.id
            )
        )
        snapshot = MetadataSnapshot(
            id=uuid.uuid4(),
            tenant_id=self.ctx.tenant_id,
            connection_id=conn.id,
            version=(current or 0) + 1,
            status="pending",
            requested_by=self.ctx.user_id,
        )
        self.session.add(snapshot)
        await self.session.commit()
        await self.audit.record(
            "metadata.sync_requested",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="connection",
            resource_id=str(conn.id),
            details={"version": snapshot.version},
        )
        return snapshot, True

    async def _latest_succeeded(self, connection_id: uuid.UUID) -> MetadataSnapshot | None:
        snapshot: MetadataSnapshot | None = await self.session.scalar(
            select(MetadataSnapshot)
            .where(
                MetadataSnapshot.connection_id == connection_id,
                MetadataSnapshot.tenant_id == self.ctx.tenant_id,
                MetadataSnapshot.status == "succeeded",
            )
            .order_by(MetadataSnapshot.version.desc())
            .limit(1)
        )
        return snapshot

    async def sync_status(self, connection_id: uuid.UUID) -> ConnectionSyncStatus:
        self.ctx.require(Permission.METADATA_READ)
        conn = await self.connections.get(connection_id)
        latest = await self.session.scalar(
            select(MetadataSnapshot)
            .where(
                MetadataSnapshot.connection_id == conn.id,
                MetadataSnapshot.tenant_id == self.ctx.tenant_id,
            )
            .order_by(MetadataSnapshot.version.desc())
            .limit(1)
        )
        active = await self._latest_succeeded(conn.id)
        return ConnectionSyncStatus(
            connection_id=conn.id,
            latest=to_status(latest) if latest is not None else None,
            active_version=active.version if active else None,
            synced_at=active.finished_at if active else None,
        )

    async def list_entities(
        self, connection_id: uuid.UUID, *, search: str | None, limit: int, offset: int
    ) -> EntityList:
        self.ctx.require(Permission.METADATA_READ)
        conn = await self.connections.get(connection_id)
        active = await self._latest_succeeded(conn.id)
        if active is None:
            return EntityList(items=[], total=0, version=None, synced_at=None)
        filters = [
            MetadataObject.snapshot_id == active.id,
            MetadataObject.tenant_id == self.ctx.tenant_id,
        ]
        if search:
            pattern = (
                "%"
                + search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            filters.append(func.lower(MetadataObject.name).like(pattern, escape="\\"))
        total = await self.session.scalar(
            select(func.count()).select_from(MetadataObject).where(*filters)
        )
        rows = await self.session.scalars(
            select(MetadataObject)
            .where(*filters)
            .order_by(MetadataObject.schema_name, MetadataObject.name)
            .limit(limit)
            .offset(offset)
        )
        return EntityList(
            items=[
                EntitySummary(
                    schema_name=o.schema_name,
                    name=o.name,
                    kind=o.kind,
                    column_count=len(o.definition.get("columns", [])),
                    row_estimate=o.row_estimate,
                    comment=o.definition.get("comment"),
                )
                for o in rows
            ],
            total=int(total or 0),
            version=active.version,
            synced_at=active.finished_at,
        )

    async def get_entity(self, connection_id: uuid.UUID, entity_name: str) -> EntityDetail:
        self.ctx.require(Permission.METADATA_READ)
        conn = await self.connections.get(connection_id)
        active = await self._latest_succeeded(conn.id)
        if active is None:
            raise NotFoundError()
        schema, _, name = entity_name.rpartition(".")
        filters = [
            MetadataObject.snapshot_id == active.id,
            MetadataObject.tenant_id == self.ctx.tenant_id,
            MetadataObject.name == name,
        ]
        if schema:
            filters.append(MetadataObject.schema_name == schema)
        matches = list(await self.session.scalars(select(MetadataObject).where(*filters).limit(10)))
        if not matches:
            raise NotFoundError()
        if len(matches) > 1:
            raise ConflictError(
                "The entity name is ambiguous; qualify it as <schema>.<name>.",
                details={"schemas": sorted(m.schema_name for m in matches)},
            )
        obj = ObjectMetadata.model_validate(matches[0].definition)
        return EntityDetail(
            schema_name=obj.schema_name,
            name=obj.name,
            kind=obj.kind.value,
            row_estimate=matches[0].row_estimate,
            comment=obj.comment,
            columns=obj.columns,
            primary_key=obj.primary_key,
            foreign_keys=obj.foreign_keys,
            version=active.version,
            fingerprint=matches[0].fingerprint,
        )


@dataclass(slots=True)
class MetadataSyncDeps:
    """What a sync job needs; built by the API (inline mode) or the worker."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    cipher: SecretCipher
    adapters: AdapterCache
    host_policy: HostPolicy
    audit: AuditService


async def _previous_fingerprints(
    session: AsyncSession, connection_id: uuid.UUID
) -> dict[tuple[str, str], str]:
    previous = await session.scalar(
        select(MetadataSnapshot.id)
        .where(
            MetadataSnapshot.connection_id == connection_id,
            MetadataSnapshot.status == "succeeded",
        )
        .order_by(MetadataSnapshot.version.desc())
        .limit(1)
    )
    if previous is None:
        return {}
    rows = await session.execute(
        select(MetadataObject.schema_name, MetadataObject.name, MetadataObject.fingerprint).where(
            MetadataObject.snapshot_id == previous
        )
    )
    return {(s, n): f for s, n, f in rows}


async def _discover(
    deps: MetadataSyncDeps,
    session: AsyncSession,
    conn: DatabaseConnection,
    snapshot: MetadataSnapshot,
) -> None:
    """Reflect the catalog and stage the new snapshot's objects on ``session``."""
    adapter = await adapter_for_connection(
        session=session,
        settings=deps.settings,
        cipher=deps.cipher,
        adapters=deps.adapters,
        host_policy=deps.host_policy,
        conn=conn,
    )
    catalog = await adapter.get_catalog([conn.schema_name] if conn.schema_name else None)
    if len(catalog.objects) > MAX_METADATA_OBJECTS:
        raise _SyncError("TOO_MANY_OBJECTS")

    old = await _previous_fingerprints(session, conn.id)
    added = changed = 0
    seen: set[tuple[str, str]] = set()
    for obj in catalog.objects:
        key = (obj.schema_name, obj.name)
        seen.add(key)
        fp = fingerprint(obj)
        if key not in old:
            added += 1
        elif old[key] != fp:
            changed += 1
        session.add(
            MetadataObject(
                tenant_id=conn.tenant_id,
                snapshot_id=snapshot.id,
                connection_id=conn.id,
                schema_name=obj.schema_name,
                name=obj.name,
                kind=obj.kind.value,
                fingerprint=fp,
                row_estimate=obj.row_estimate,
                definition=obj.model_dump(mode="json"),
            )
        )
    snapshot.status = "succeeded"
    snapshot.server_version = (catalog.server_version or "")[:255] or None
    snapshot.object_count = len(catalog.objects)
    snapshot.added_count = added
    snapshot.changed_count = changed
    snapshot.removed_count = len(set(old) - seen)


async def run_snapshot(deps: MetadataSyncDeps, snapshot_id: uuid.UUID) -> None:
    """Execute one metadata sync. Idempotent: a snapshot not in ``pending`` is skipped."""
    started = time.perf_counter()
    async with deps.session_factory() as session:
        snapshot = await session.get(MetadataSnapshot, snapshot_id)
        if snapshot is None or snapshot.status != "pending":
            logger.info("metadata sync skipped", extra={"snapshot_id": str(snapshot_id)})
            return
        conn = await session.get(DatabaseConnection, snapshot.connection_id)
        if conn is None or conn.deleted_at is not None or conn.tenant_id != snapshot.tenant_id:
            snapshot.status = "failed"
            snapshot.error_code = "CONNECTION_GONE"
            snapshot.finished_at = utcnow()
            await session.commit()
            return
        snapshot.status = "running"
        snapshot.started_at = utcnow()
        await session.commit()
        tenant_id, connection_id, connector = conn.tenant_id, conn.id, conn.connector_type

        error_code: str | None = None
        try:
            await _discover(deps, session, conn, snapshot)
        except Exception as exc:  # noqa: BLE001 - any failure must end in a terminal state
            error_code = _error_code(exc)
            logger.warning(
                "metadata sync failed",
                extra={
                    "snapshot_id": str(snapshot_id),
                    "error_code": error_code,
                    "error_type": type(exc).__name__,
                },
            )
            await session.rollback()
            snapshot = await session.get(MetadataSnapshot, snapshot_id)
            if snapshot is None:  # pragma: no cover - deleted concurrently
                return
            snapshot.status = "failed"
            snapshot.error_code = error_code
        snapshot.finished_at = utcnow()
        await session.commit()
        outcome = "succeeded" if error_code is None else "failed"
        METADATA_SYNC_DURATION.labels(connector=connector, outcome=outcome).observe(
            time.perf_counter() - started
        )
        details = {
            "version": snapshot.version,
            "objects": snapshot.object_count,
            "error_code": error_code,
        }

    await deps.audit.record(
        "metadata.sync_completed",
        AuditOutcome.SUCCESS if error_code is None else AuditOutcome.ERROR,
        tenant_id=tenant_id,
        resource_type="connection",
        resource_id=str(connection_id),
        details=details,
    )


class _SyncError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, _SyncError):
        return exc.code
    if isinstance(exc, AppError):
        nested = exc.details.get("error_code") or exc.details.get("reason")
        return str(nested or exc.code)[:64]
    return "UNKNOWN"
