"""Celery application for the ERP Insight AI worker.

Run:  celery -A erp_worker.main:celery_app worker -Q default,metadata,ingestion,queries,exports

Security/robustness defaults:
* JSON-only serialisation (no pickle -> no deserialisation RCE via the broker).
* ``task_acks_late`` + ``task_reject_on_worker_lost`` so a crashed worker's tasks
  are redelivered; tasks must therefore be idempotent.
* ``worker_prefetch_multiplier=1`` so long analytical tasks are not hoarded.
* Global soft/hard time limits.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging, task_postrun, task_prerun

from app.config import get_settings
from app.core.logging import configure_logging, get_logger, request_id_var
from app.workers.queues import (
    DEFAULT_HARD_TIME_LIMIT_SECONDS,
    DEFAULT_SOFT_TIME_LIMIT_SECONDS,
    EXPORT_PURGE_INTERVAL_SECONDS,
    TASK_ROUTES,
    Queue,
    TaskName,
)

logger = get_logger(__name__)


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "erp-insight-worker", broker=settings.broker_url, backend=settings.result_backend_url
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        result_expires=3600,
        task_routes=TASK_ROUTES,
        task_default_queue=Queue.DEFAULT.value,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_track_started=True,
        task_soft_time_limit=DEFAULT_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=DEFAULT_HARD_TIME_LIMIT_SECONDS,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=500,
        worker_hijack_root_logger=False,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
        # Run with `celery -A erp_worker.main:celery_app beat` (one instance).
        beat_schedule={
            "purge-expired-exports": {
                "task": TaskName.EXPORT_PURGE.value,
                "schedule": EXPORT_PURGE_INTERVAL_SECONDS,
            }
        },
    )
    app.autodiscover_tasks(["erp_worker"], related_name="tasks", force=True)
    return app


@setup_logging.connect
def _configure_logging(**_: Any) -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service="erp-insight-worker"
    )


@task_prerun.connect
def _bind_request_id(task: Any = None, **_: Any) -> None:
    request = getattr(task, "request", None)
    request_id = getattr(request, "request_id", None) if request is not None else None
    request_id_var.set(request_id if isinstance(request_id, str) else None)


@task_postrun.connect
def _clear_request_id(**_: Any) -> None:
    request_id_var.set(None)


celery_app = create_celery_app()
