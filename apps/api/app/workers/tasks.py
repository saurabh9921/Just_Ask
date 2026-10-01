"""Producer-side task dispatch.

``TaskDispatcher`` sends tasks *by name* through a Celery client that has no task
implementations registered. Payloads are validated Pydantic models serialised to
JSON (the only serializer the worker accepts), never pickled objects.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from pydantic import BaseModel

from app.config import Settings
from app.core.logging import get_logger, request_id_var
from app.workers.queues import TASK_ROUTES, TaskName

logger = get_logger(__name__)


def build_celery_client(settings: Settings) -> Celery:
    client = Celery(
        "erp-insight-api", broker=settings.broker_url, backend=settings.result_backend_url
    )
    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_routes=TASK_ROUTES,
        broker_connection_retry_on_startup=True,
    )
    return client


class TaskDispatcher:
    def __init__(self, client: Celery) -> None:
        self._client = client

    def enqueue(self, task: TaskName, payload: BaseModel | None = None, **options: Any) -> str:
        """Enqueue ``task`` and return its task ID.

        The current request ID is propagated as a header so worker logs can be
        correlated with the originating API request.
        """
        kwargs = {"payload": payload.model_dump(mode="json")} if payload is not None else {}
        headers = {"request_id": request_id_var.get()}
        result = self._client.send_task(task.value, kwargs=kwargs, headers=headers, **options)
        logger.info("task enqueued", extra={"task": task.value, "task_id": result.id})
        return str(result.id)
