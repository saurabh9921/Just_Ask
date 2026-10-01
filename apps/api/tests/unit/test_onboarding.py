"""Onboarding a live database end to end through the public API.

connect -> test -> metadata sync -> draft entities from the *raw* tables -> ingest ->
ask a question. Nothing about the database is described in advance: this is the
path an operator takes against a real ERP.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI

from app import onboarding
from app.config import Settings
from app.core.permissions import Role
from app.onboarding import OnboardingClient, OnboardingError, draft_for_connection, select_objects
from tests.conftest import (
    DEFAULT_PASSWORD,
    AppFactory,
    bearer,
    client_for,
    create_account,
    login,
)
from tests.fixtures.demo_erp import seed_demo

PRODUCT_LAYER = Path(__file__).resolve().parents[4] / "semantic-layer"


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    root = tmp_path / "customer-layer"
    shutil.copytree(PRODUCT_LAYER, root)
    return root


@pytest.fixture
async def live_app(
    app_factory: AppFactory, prepared_db: Settings, layer: Path, sqlite_data_dir: Path
) -> FastAPI:
    await seed_demo(
        f"sqlite+aiosqlite:///{(sqlite_data_dir / 'erp.db').as_posix()}",
        anchor_date=date.today(),  # noqa: DTZ011 - demo data relative to today
        tenant_code="acme",
    )
    return app_factory(
        settings_override=prepared_db.model_copy(update={"semantic_layer_path": str(layer)})
    )


async def test_onboard_a_database_nobody_described(
    live_app: FastAPI, layer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding, "POLL_SECONDS", 0.05)
    await create_account(
        live_app, tenant_slug="acme", email="admin@acme.test", roles=[Role.DATA_ADMIN]
    )
    analyst = await create_account(
        live_app, tenant_slug="acme", email="an@acme.test", roles=[Role.ANALYST]
    )
    async with client_for(live_app) as http:
        client = await OnboardingClient.login(
            http, tenant="acme", email="admin@acme.test", password=DEFAULT_PASSWORD
        )
        conn = await client.create_connection(
            {"name": "ERP production", "connector_type": "sqlite", "database_name": "erp.db"}
        )
        assert (await client.test_connection(conn["id"]))["ok"] is True
        sync = await client.sync_metadata(conn["id"], wait_seconds=60)
        assert sync["status"] == "succeeded" and sync["object_count"] > 10

        drafts, written = await draft_for_connection(
            client,
            conn,
            layer_root=layer,
            patterns="sales_orders,sales_order_lines,customers",
            overwrite=False,
        )
        assert {d.entity for d in drafts.entities} == {
            "SalesOrders",
            "SalesOrderLines",
            "Customers",
        }
        assert any(p.parent.name == "joins" for p in written)

        latest = await client.ingest(conn["id"], wait_seconds=60)
        assert latest["status"] == "active"
        assert {"SalesOrders", "SalesOrderLines", "Customers"} <= set(latest["entity_bindings"])

        headers = bearer(await login(http, analyst))
        r = await http.post(
            "/api/v1/analytics/query",
            json={
                "question": "How many sales orders by status in the last 12 months?",
                "connection_id": conn["id"],
            },
            headers=headers,
        )
        body = r.json()
        assert r.status_code == 200, body
        assert body["status"] == "succeeded", body
        assert "sales_orders" in body["sql"] and body["result"]["row_count"] > 0
        # Sensitive columns of the live table stay out of reach.
        r = await http.post(
            "/api/v1/analytics/query",
            json={"question": "List customer email addresses", "connection_id": conn["id"]},
            headers=headers,
        )
        assert r.json()["status"] == "refused"


async def test_drafting_needs_an_object_filter_for_large_schemas() -> None:
    objects = [{"schema_name": "dbo", "name": f"t{i}", "kind": "table"} for i in range(60)]
    with pytest.raises(OnboardingError, match="--objects"):
        select_objects(objects, None)
    assert len(select_objects(objects, "t1*")) == 11
    assert select_objects(objects, "DBO.T5") == [objects[5]]
    with pytest.raises(OnboardingError):
        select_objects(objects, "nothing_*")


async def test_errors_are_reported_without_raw_responses(live_app: FastAPI) -> None:
    async with client_for(live_app) as http:
        with pytest.raises(OnboardingError, match="sign-in failed - INVALID_CREDENTIALS"):
            await OnboardingClient.login(
                http, tenant="acme", email="nobody@acme.test", password="wrong-password-123"
            )


def test_passwords_are_never_command_line_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        onboarding.main(
            ["--tenant", "t", "--email", "e", "ingest", "--name", "x", "--password", "p"]
        )
    assert "unrecognized arguments: --password" in capsys.readouterr().err


def test_missing_identity_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ERP_TENANT", raising=False)
    monkeypatch.delenv("ERP_ADMIN_EMAIL", raising=False)
    assert onboarding.main(["status", "--name", "x"]) == 1
    assert "--tenant and --email are required" in capsys.readouterr().err


def test_unreachable_api_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ERP_ADMIN_PASSWORD", "x" * 16)
    code = onboarding.main(
        [
            "--api-url",
            "http://127.0.0.1:9",
            "--tenant",
            "t",
            "--email",
            "e@x",
            "status",
            "--name",
            "x",
        ]
    )
    assert code == 2 and "cannot reach the API" in capsys.readouterr().err
