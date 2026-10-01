"""Background export tasks (queue: ``exports``).

``export.run`` produces one export file; ``export.purge`` (Celery beat, every
``EXPORT_PURGE_INTERVAL_SECONDS``) deletes expired files. Both are idempotent:
``run_export`` only processes ``pending`` jobs and purging an already-expired job is a
no-op, so redelivery (acks_late) is safe.
"""

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
from app.semantic.registry import SemanticLayerRegistry
from app.services.audit_service import AuditService
from app.services.export_service import ExportDeps, purge_expired_exports, run_export
from app.services.secrets_service import SecretCipher
from app.workers.jobs import ExportPayload
from app.workers.queues import (
    EXPORT_HARD_TIME_LIMIT_SECONDS,
    EXPORT_SOFT_TIME_LIMIT_SECONDS,
    TaskName,
)

logger = get_logger(__name__)


async def _run(export_id: uuid.UUID) -> None:
    settings = get_settings()
    database = Database(settings)
    adapters = AdapterCache(ConnectorFactory(settings))
    try:
        await run_export(
            ExportDeps(
                settings=settings,
                session_factory=database.session_factory,
                cipher=SecretCipher(settings),
                adapters=adapters,
                host_policy=HostPolicy(settings.connection_host_denylist),
                audit=AuditService(database.session_factory),
                registry=SemanticLayerRegistry(settings.semantic_layer_path),
            ),
            export_id,
        )
    finally:
        await adapters.close_all()
        await database.dispose()


async def _purge() -> int:
    settings = get_settings()
    database = Database(settings)
    try:
        return await purge_expired_exports(settings, database.session_factory)
    finally:
        await database.dispose()


@shared_task(
    name=TaskName.EXPORT_RUN.value,
    ignore_result=True,
    soft_time_limit=EXPORT_SOFT_TIME_LIMIT_SECONDS,
    time_limit=EXPORT_HARD_TIME_LIMIT_SECONDS,
)
def run_export_task(payload: dict[str, Any]) -> None:
    export_id = ExportPayload.model_validate(payload).export_id
    logger.info("export started", extra={"export_id": str(export_id)})
    asyncio.run(_run(export_id))


@shared_task(name=TaskName.EXPORT_PURGE.value, ignore_result=True)
def purge_exports_task() -> int:
    removed = asyncio.run(_purge())
    if removed:
        logger.info("expired exports purged", extra={"count": removed})
    return removed
