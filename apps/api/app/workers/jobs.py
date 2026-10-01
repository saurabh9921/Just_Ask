"""Background job submission for the API process.

``METADATA_SYNC_MODE=celery`` (production): jobs are enqueued by task name for the
worker fleet. ``inline`` (development/tests): jobs run as asyncio tasks inside the
API process - convenient without a worker, but not durable across restarts.
The same mode governs metadata sync and semantic ingestion.

A job left ``pending``/``running`` by a crash or restart would otherwise block its
connection forever (requests return the job already in flight); ``is_abandoned``
lets the next request supersede it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel

from app.core.logging import get_logger
from app.core.security import utcnow
from app.workers.queues import TaskName
from app.workers.tasks import TaskDispatcher

logger = get_logger(__name__)

# Well beyond the worker's hard time limit (queues.DEFAULT_HARD_TIME_LIMIT_SECONDS), so a
# job this old is dead, never merely slow.
STALE_JOB_AFTER: Final = timedelta(minutes=15)


def is_abandoned(created_at: datetime, started_at: datetime | None) -> bool:
    """True when an in-flight job has shown no progress for ``STALE_JOB_AFTER``."""
    reference = started_at or created_at
    if reference.tzinfo is None:  # SQLite returns naive UTC timestamps
        reference = reference.replace(tzinfo=UTC)
    return utcnow() - reference > STALE_JOB_AFTER


class MetadataSyncPayload(BaseModel):
    snapshot_id: uuid.UUID


class SemanticIngestPayload(BaseModel):
    version_id: uuid.UUID


class ExportPayload(BaseModel):
    export_id: uuid.UUID


class JobRunner:
    def __init__(
        self,
        *,
        mode: str,
        task: TaskName,
        payload: Callable[[uuid.UUID], BaseModel],
        inline: Callable[[uuid.UUID], Awaitable[None]] | None = None,
        dispatcher: TaskDispatcher | None = None,
    ) -> None:
        if mode == "celery" and dispatcher is None:
            raise ValueError("celery mode requires a TaskDispatcher")
        if mode == "inline" and inline is None:
            raise ValueError("inline mode requires a job function")
        self.mode = mode
        self._task = task
        self._payload = payload
        self._inline = inline
        self._dispatcher = dispatcher
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, job_id: uuid.UUID) -> None:
        if self.mode == "celery":
            assert self._dispatcher is not None  # noqa: S101 - checked in __init__
            self._dispatcher.enqueue(self._task, self._payload(job_id))
            return
        task = asyncio.create_task(self._run_inline(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_inline(self, job_id: uuid.UUID) -> None:
        assert self._inline is not None  # noqa: S101 - checked in __init__
        try:
            await self._inline(job_id)
        except Exception:
            logger.exception(
                "inline job crashed", extra={"task": self._task.value, "job_id": str(job_id)}
            )

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
