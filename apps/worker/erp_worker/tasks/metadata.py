"""Metadata synchronisation task (queue: ``metadata``)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from celery import shared_task

from app.config import get_settings
from app.connectors.common.network import HostPolicy
from app.connectors.factory import AdapterCache, ConnectorFactory
from app.core.logging import get_logger
from app.db.session import Database
from app.services.audit_service import AuditService
from app.services.metadata_service import MetadataSyncDeps, run_snapshot
from app.services.secrets_service import SecretCipher
from app.workers.jobs import MetadataSyncPayload
from app.workers.queues import TaskName

logger = get_logger(__name__)


async def _run(snapshot_id: uuid.UUID) -> None:
    settings = get_settings()
    database = Database(settings)
    adapters = AdapterCache(ConnectorFactory(settings))
    try:
        await run_snapshot(
            MetadataSyncDeps(
                settings=settings,
                session_factory=database.session_factory,
                cipher=SecretCipher(settings),
                adapters=adapters,
                host_policy=HostPolicy(settings.connection_host_denylist),
                audit=AuditService(database.session_factory),
            ),
            snapshot_id,
        )
    finally:
        await adapters.close_all()
        await database.dispose()


@shared_task(name=TaskName.METADATA_SYNC.value, ignore_result=True)
def sync_metadata(payload: dict[str, Any]) -> None:
    """Idempotent: ``run_snapshot`` skips snapshots that are no longer pending, so a
    redelivered message (acks_late) cannot create duplicate objects."""
    snapshot_id = MetadataSyncPayload.model_validate(payload).snapshot_id
    logger.info("metadata sync started", extra={"snapshot_id": str(snapshot_id)})
    asyncio.run(_run(snapshot_id))
