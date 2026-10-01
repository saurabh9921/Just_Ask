"""Operational tasks."""

from __future__ import annotations

from datetime import UTC, datetime

from celery import shared_task

from app.core.logging import get_logger
from app.workers.queues import TaskName

logger = get_logger(__name__)


@shared_task(name=TaskName.PING.value, ignore_result=False)
def ping() -> dict[str, str]:
    """Round-trip check used by readiness probes and smoke tests."""
    now = datetime.now(tz=UTC).isoformat()
    logger.info("ping task executed")
    return {"status": "ok", "worker_time": now}
