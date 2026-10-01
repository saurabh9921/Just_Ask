from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.core.logging import request_id_var
from app.workers.queues import TASK_ROUTES, TaskName
from app.workers.tasks import TaskDispatcher, build_celery_client


@dataclass
class _Result:
    id: str


@dataclass
class FakeCelery:
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, name: str, **kwargs: Any) -> _Result:
        self.sent.append({"name": name, **kwargs})
        return _Result(id="task-1")


class Payload(BaseModel):
    connection_id: str
    full: bool = False


def test_enqueue_sends_by_name_with_json_payload_and_request_id() -> None:
    fake = FakeCelery()
    token = request_id_var.set("req_dispatch")
    try:
        task_id = TaskDispatcher(fake).enqueue(TaskName.PING, Payload(connection_id="c1"))  # type: ignore[arg-type]
    finally:
        request_id_var.reset(token)
    assert task_id == "task-1"
    (sent,) = fake.sent
    assert sent["name"] == "system.ping"
    assert sent["kwargs"] == {"payload": {"connection_id": "c1", "full": False}}
    assert sent["headers"] == {"request_id": "req_dispatch"}


def test_every_task_has_a_route() -> None:
    assert {t.value for t in TaskName} == set(TASK_ROUTES)


def test_celery_client_is_json_only(settings: Settings) -> None:
    client = build_celery_client(settings)
    assert client.conf.accept_content == ["json"]
    assert client.conf.task_serializer == "json"
