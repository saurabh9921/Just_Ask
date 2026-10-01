from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")

from app.config import get_settings
from app.workers.queues import TASK_ROUTES, TaskName
from erp_worker.main import celery_app


def test_worker_accepts_json_only() -> None:
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"


def test_worker_reliability_defaults() -> None:
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_time_limit > conf.task_soft_time_limit


def test_every_declared_task_is_registered() -> None:
    celery_app.loader.import_default_modules()
    registered = set(celery_app.tasks.keys())
    assert {t.value for t in TaskName} <= registered
    assert set(TASK_ROUTES) <= registered


def test_ping_task_runs() -> None:
    result = celery_app.tasks[TaskName.PING.value].apply()
    assert result.successful()
    assert result.get()["status"] == "ok"


def test_export_tasks_are_scheduled_routed_and_time_limited() -> None:
    schedule = celery_app.conf.beat_schedule["purge-expired-exports"]
    assert schedule["task"] == TaskName.EXPORT_PURGE.value
    assert TASK_ROUTES[TaskName.EXPORT_RUN.value]["queue"] == "exports"
    assert TASK_ROUTES[TaskName.EXPORT_PURGE.value]["queue"] == "exports"
    task = celery_app.tasks[TaskName.EXPORT_RUN.value]
    # The service-level export timeout must fire before Celery kills the task, so the job
    # still ends in a terminal state with a proper error code.
    soft, hard = task.soft_time_limit, task.time_limit
    assert soft is not None and hard is not None
    assert get_settings().export_timeout_seconds < soft < hard
