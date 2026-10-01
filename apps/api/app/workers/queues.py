"""Queue names, task names and routing shared by the API (producer) and the worker.

The API never imports worker code. It enqueues by *task name* (see
``app.workers.tasks``), so the two deployables can be scaled and released
independently as long as task names and payload schemas stay compatible.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Queue(StrEnum):
    DEFAULT = "default"
    METADATA = "metadata"  # metadata sync / snapshotting (Phase 2)
    INGESTION = "ingestion"  # semantic-layer -> Qdrant ingestion (Phase 3)
    QUERIES = "queries"  # long-running analytical queries (Phase 4/6)
    EXPORTS = "exports"  # large async exports (Phase 6)


class TaskName(StrEnum):
    PING = "system.ping"
    METADATA_SYNC = "metadata.sync"
    SEMANTIC_INGEST = "semantic.ingest"
    EXPORT_RUN = "export.run"
    EXPORT_PURGE = "export.purge"


TASK_ROUTES: Final[dict[str, dict[str, str]]] = {
    TaskName.PING.value: {"queue": Queue.DEFAULT.value},
    TaskName.METADATA_SYNC.value: {"queue": Queue.METADATA.value},
    TaskName.SEMANTIC_INGEST.value: {"queue": Queue.INGESTION.value},
    TaskName.EXPORT_RUN.value: {"queue": Queue.EXPORTS.value},
    TaskName.EXPORT_PURGE.value: {"queue": Queue.EXPORTS.value},
}

# Hard ceilings so a stuck task can never hold a worker forever.
DEFAULT_SOFT_TIME_LIMIT_SECONDS: Final = 300
DEFAULT_HARD_TIME_LIMIT_SECONDS: Final = 330
# Exports stream up to EXPORT_MAX_ROWS rows; their own limits sit above EXPORT_TIMEOUT_SECONDS.
EXPORT_SOFT_TIME_LIMIT_SECONDS: Final = 900
EXPORT_HARD_TIME_LIMIT_SECONDS: Final = 960
# Expired export files are purged on this interval (Celery beat).
EXPORT_PURGE_INTERVAL_SECONDS: Final = 300.0
