"""Phase 6 background exports: API, job, download tokens, expiry and the writer."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.permissions import Role
from app.core.security import utcnow
from app.db.models import AuditEvent, ExportJob
from app.services.export_service import purge_expired_exports
from app.services.export_writer import (
    ExportWriter,
    SensitiveDataDetectedError,
    csv_cell,
    sensitive_kind,
)
from tests.conftest import bearer, create_account, login, resources_of
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today, script_plans
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]


async def analyst_headers(
    app: FastAPI, client: httpx.AsyncClient, email: str = "an@acme.test"
) -> dict[str, str]:
    return bearer(
        await login(
            client,
            await create_account(app, tenant_slug="acme", email=email, roles=[Role.ANALYST]),
        )
    )


async def export(
    client: httpx.AsyncClient, headers: dict[str, str], query_id: str, **body: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/exports", json={"query_id": query_id, "confirm": True, **body}, headers=headers
    )


async def finished(
    app: FastAPI, client: httpx.AsyncClient, headers: dict[str, str], export_id: str
) -> dict[str, Any]:
    await resources_of(app).export_jobs.wait_idle()
    body: dict[str, Any] = (
        await client.get(f"/api/v1/exports/{export_id}", headers=headers)
    ).json()
    return body


async def download(
    client: httpx.AsyncClient, headers: dict[str, str], export_id: str
) -> httpx.Response:
    token = await client.post(f"/api/v1/exports/{export_id}/download-token", headers=headers)
    assert token.status_code == 200, token.text
    return await client.get(token.json()["url"])


def csv_files(root: str) -> list[Path]:
    return list(Path(root).rglob("*.csv"))


async def audit_types(app: FastAPI) -> list[str]:
    async with resources_of(app).database.session_factory() as session:
        return [e.event_type for e in await session.scalars(select(AuditEvent))]


# ------------------------------------------------------------------ API + job
async def test_csv_export_end_to_end(app: FastAPI, client: httpx.AsyncClient, demo_db: str) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]

    unconfirmed = await client.post("/api/v1/exports", json={"query_id": qid}, headers=analyst)
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    created = await export(client, analyst, qid)
    assert created.status_code == 202, created.text
    job = await finished(app, client, analyst, created.json()["export_id"])
    assert job["status"] == "succeeded", job
    assert job["row_count"] == 4 and job["truncated"] is False
    assert job["file_name"].startswith("erp-insight-show-total-sales-by-region")

    response = await download(client, analyst, job["export_id"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["Region", "Total sales"]
    assert len(rows) == 5 and {r[0] for r in rows[1:]} == {"North", "South", "East", "West"}

    listed = (await client.get("/api/v1/exports", headers=analyst)).json()
    assert listed["items"][0]["downloaded_at"] is not None
    types = await audit_types(app)
    for event in ("export.requested", "export.completed", "export.downloaded"):
        assert event in types


async def test_download_tokens_are_single_use(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    job = await finished(
        app, client, analyst, (await export(client, analyst, qid)).json()["export_id"]
    )
    token = (
        await client.post(f"/api/v1/exports/{job['export_id']}/download-token", headers=analyst)
    ).json()
    assert token["expires_in"] == 120
    assert (await client.get(token["url"])).status_code == 200
    assert (await client.get(token["url"])).status_code == 404
    assert (await client.get("/api/v1/exports/download/not-a-token")).status_code == 404
    bearer_file = await client.get(f"/api/v1/exports/{job['export_id']}/file", headers=analyst)
    assert bearer_file.status_code == 200


async def test_json_export_and_all_rows_scope(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    script_plans(
        app,
        lambda _: {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["customer"],
            "date_range": {"field": "order_date", "relative": "last_12_months"},
            "sort": {"by": "total_sales", "direction": "desc"},
            "limit": 5,
        },
    )
    qid = (await ask(client, analyst, cid, "top customers")).json()["query_id"]
    same = await finished(
        app,
        client,
        analyst,
        (await export(client, analyst, qid, format="json")).json()["export_id"],
    )
    assert same["row_count"] == 5 and same["truncated"] is False  # top 5 is the question
    everything = await finished(
        app,
        client,
        analyst,
        (await export(client, analyst, qid, scope="all_rows")).json()["export_id"],
    )
    assert everything["row_count"] > 5 and everything["truncated"] is False

    body = json.loads((await download(client, analyst, same["export_id"])).content)
    assert [c["name"] for c in body["columns"]] == ["customer", "total_sales"]
    assert len(body["rows"]) == 5 and body["row_count"] == 5 and body["truncated"] is False
    assert body["question"] == "top customers"


async def test_comparison_exports_label_the_period(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, "Compare this month's sales with last month")).json()[
        "query_id"
    ]
    job = await finished(
        app, client, analyst, (await export(client, analyst, qid)).json()["export_id"]
    )
    rows = list(
        csv.reader(
            io.StringIO(
                (await download(client, analyst, job["export_id"])).content.decode("utf-8-sig")
            )
        )
    )
    assert rows[0][0] == "Period"
    assert [r[0] for r in rows[1:]] == ["current", "previous"]


async def test_export_requires_permission_and_ownership(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    other = await analyst_headers(app, client, "an2@acme.test")
    viewer_qid = (await ask(client, viewer, cid, REGION_Q)).json()["query_id"]
    denied = await export(client, viewer, viewer_qid)
    assert denied.status_code == 403
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    assert (await export(client, other, qid)).status_code == 404  # not their query
    job = await finished(
        app, client, analyst, (await export(client, analyst, qid)).json()["export_id"]
    )
    eid = job["export_id"]
    assert (await client.get(f"/api/v1/exports/{eid}", headers=other)).status_code == 404
    assert (
        await client.post(f"/api/v1/exports/{eid}/download-token", headers=other)
    ).status_code == 404
    assert (await client.get(f"/api/v1/exports/{eid}/file", headers=other)).status_code == 404
    assert (await client.delete(f"/api/v1/exports/{eid}", headers=other)).status_code == 404


async def test_only_answered_questions_are_exportable(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    refused = (await ask(client, analyst, cid, "Show customer bank account numbers")).json()
    r = await export(client, analyst, refused["query_id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "QUERY_NOT_EXPORTABLE"


async def test_concurrent_export_limit(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    resources = resources_of(app)

    async def hold(_export_id: Any) -> None:  # jobs stay pending
        return None

    monkeypatch.setattr(resources.export_jobs, "submit", hold)
    monkeypatch.setattr(resources.settings, "export_max_active_per_user", 1)
    assert (await export(client, analyst, qid)).status_code == 202
    second = await export(client, analyst, qid)
    assert second.status_code == 409 and second.json()["error"]["code"] == "TOO_MANY_EXPORTS"


async def test_expired_exports_are_purged(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    job = await finished(
        app, client, analyst, (await export(client, analyst, qid)).json()["export_id"]
    )
    resources = resources_of(app)
    files = csv_files(resources.settings.export_dir)
    assert len(files) == 1
    async with resources.database.session_factory() as session:
        await session.execute(update(ExportJob).values(expires_at=utcnow() - timedelta(seconds=1)))
        await session.commit()
    token_attempt = await client.post(
        f"/api/v1/exports/{job['export_id']}/download-token", headers=analyst
    )
    assert token_attempt.status_code == 410
    assert await purge_expired_exports(resources.settings, resources.database.session_factory) == 1
    assert not files[0].exists()
    after = (await client.get(f"/api/v1/exports/{job['export_id']}", headers=analyst)).json()
    assert after["status"] == "expired"


async def test_delete_removes_the_file(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    job = await finished(
        app, client, analyst, (await export(client, analyst, qid)).json()["export_id"]
    )
    export_dir = resources_of(app).settings.export_dir
    assert csv_files(export_dir)
    assert (
        await client.delete(f"/api/v1/exports/{job['export_id']}", headers=analyst)
    ).status_code == 204
    assert not csv_files(export_dir)
    assert (
        await client.get(f"/api/v1/exports/{job['export_id']}/file", headers=analyst)
    ).status_code == 410


async def test_job_revalidates_the_user_at_run_time(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user is deactivated between request and run, nothing is written."""
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    qid = (await ask(client, analyst, cid, REGION_Q)).json()["query_id"]
    resources = resources_of(app)
    submitted: list[Any] = []

    async def defer(export_id: Any) -> None:
        submitted.append(export_id)

    monkeypatch.setattr(resources.export_jobs, "submit", defer)
    eid = (await export(client, analyst, qid)).json()["export_id"]
    from app.db.models import User

    async with resources.database.session_factory() as session:
        await session.execute(
            update(User).where(User.email == "an@acme.test").values(is_active=False)
        )
        await session.commit()
    from app.services.export_service import ExportDeps, run_export

    deps = ExportDeps(
        settings=resources.settings,
        session_factory=resources.database.session_factory,
        cipher=resources.cipher,
        adapters=resources.adapters,
        host_policy=resources.host_policy,
        audit=resources.audit,
        registry=resources.semantic_registry,
    )
    await run_export(deps, submitted[0])
    async with resources.database.session_factory() as session:
        job = await session.get(ExportJob, submitted[0])
    assert job is not None and job.status == "failed" and job.error_code == "SESSION_REVOKED"
    assert not csv_files(resources.settings.export_dir)
    assert str(submitted[0]) == eid


# ------------------------------------------------------------------ writer
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("card 4111 1111 1111 1111 on file", "card_number"),
        ("4111-1111-1111-1111", "card_number"),
        ("IBAN DE89 3704 0044 0532 0130 00", "iban"),
        ("GB82WEST12345698765432", "iban"),
        ("Order SO-2026-000123", None),
        ("1234567890123", None),  # not Luhn-valid
        ("0000000000000000", None),  # repeated digits are not cards
        ("Granite Manufacturing 006", None),
    ],
)
def test_sensitive_value_detection(text: str, kind: str | None) -> None:
    assert sensitive_kind(text) == kind


def test_writer_aborts_on_sensitive_values(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    with (
        pytest.raises(SensitiveDataDetectedError),
        ExportWriter(path, "csv", [("note", "Note")]) as writer,
    ):
        writer.write_batch([("fine",), ("paid with 4111111111111111",)])


def test_csv_cells_are_formula_safe() -> None:
    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert csv_cell(-5) == "-5"
    assert csv_cell(None) == ""


async def test_all_rows_exports_flag_the_platform_cap(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, cid = await setup_indexed(app, client, demo_db)
    analyst = await analyst_headers(app, client)
    script_plans(
        app,
        lambda _: {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["customer"],
            "date_range": {"field": "order_date", "relative": "last_12_months"},
            "limit": 5,
        },
    )
    qid = (await ask(client, analyst, cid, "customers")).json()["query_id"]
    monkeypatch.setattr(resources_of(app).settings, "export_max_rows", 7)
    job = await finished(
        app,
        client,
        analyst,
        (await export(client, analyst, qid, scope="all_rows")).json()["export_id"],
    )
    assert job["row_count"] == 7 and job["truncated"] is True
