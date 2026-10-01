"""Validation-first answers end to end (demo ERP, mock model).

``Invoice`` → ``Customer`` has the same shape as the live ``Leads`` → ``Companies``:
a subject with its own name field and a key into a master entity. Expected values are
computed with SQL directly against the demo database, never taken from the API.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from tests.unit.test_analytics_api import ask, demo_db, fixed_today
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]


def _db(sqlite_data_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(sqlite_data_dir / "demo.db")


def _customer_with_invoices(sqlite_data_dir: Path) -> tuple[str, int]:
    with _db(sqlite_data_dir) as db:
        name, n = db.execute(
            "SELECT customer_name, count(*) FROM analytics_invoices WHERE tenant_code = 'acme' "
            "GROUP BY customer_name ORDER BY count(*) DESC, customer_name LIMIT 1"
        ).fetchone()
    return str(name), int(n)


def _check_traceability(analysis: dict[str, Any]) -> None:
    """Every metric names its query; every insight states only known metrics/queries
    (or, for a data gap, the metrics that are unavailable)."""
    labels = {q["label"] for q in analysis["queries"]}
    keys = {m["key"] for m in analysis["metrics"]} | {u["key"] for u in analysis["unavailable"]}
    for metric in analysis["metrics"]:
        assert metric["query"] in labels, metric
        assert metric["source"] == "query_result" and metric["validated"] is True
    for insight in analysis["insights"]:
        assert set(insight["metrics"]) <= keys | labels, insight
    for query in analysis["queries"]:
        assert query["sql"].lstrip().upper().startswith("SELECT"), query


async def test_analysis_for_a_named_customer(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, sqlite_data_dir: Path
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    name, expected = _customer_with_invoices(sqlite_data_dir)
    body = (await ask(client, viewer, cid, f"give me invoice analysis for {name}")).json()
    assert body["status"] == "succeeded", body
    analysis = body["analysis"]
    assert analysis["intent"] == "invoice_analysis"
    assert analysis["title"] == f"Invoice Analysis — {name}"
    entity = analysis["entity"]
    assert entity["resolved"] is True and entity["match"] == "exact" and entity["name"] == name
    assert entity["filter_field"] == "customer_id"  # filtered through the master key
    assert analysis["validation"]["entity_resolved"] is True
    assert analysis["validation"]["entity_filter_applied"] is True
    total = next(m for m in analysis["metrics"] if m["key"] == "total")
    assert total["value"] == expected  # matches SQL against the demo database
    assert all("customer_id" in q["sql"] for q in analysis["queries"])  # every query filtered
    _check_traceability(analysis)
    chart = analysis["chart"]
    if chart["enabled"]:
        assert chart["validation"]["valid"] and len(chart["data"]) >= 2
        assert all(p["label"].strip() and p["label"] != "(blank)" for p in chart["data"])
    else:
        assert chart["validation"]["reasons"]
    assert analysis["insights"][0]["text"] == (
        f"{name} has {expected:,} {'invoice' if expected == 1 else 'invoices'}."
    )
    # The answer is stated from validated metrics, not by the model.
    assert body["explanation"]["source"] == "system"


async def test_analysis_without_a_name_covers_everything(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, sqlite_data_dir: Path
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    with _db(sqlite_data_dir) as db:
        (expected,) = db.execute(
            "SELECT count(*) FROM analytics_invoices WHERE tenant_code = 'acme'"
        ).fetchone()
    analysis = (await ask(client, viewer, cid, "invoice analysis")).json()["analysis"]
    assert analysis["entity"] is None and analysis["validation"]["entity_resolved"] is None
    assert next(m for m in analysis["metrics"] if m["key"] == "total")["value"] == expected
    status = next(m for m in analysis["metrics"] if m["key"] == "invoices_by_status")
    with _db(sqlite_data_dir) as db:
        statuses = db.execute(
            "SELECT count(DISTINCT status) FROM analytics_invoices "
            "WHERE tenant_code = 'acme' AND status IS NOT NULL AND trim(status) <> ''"
        ).fetchone()[0]
    assert status["value"] == statuses
    _check_traceability(analysis)


async def test_an_unknown_customer_is_never_analysed(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    body = (await ask(client, viewer, cid, "invoice analysis for Zyx Nonexistent Traders")).json()
    assert body["status"] == "clarification", body
    assert body["clarification_question"].startswith(
        "I couldn't find a customer named “Zyx Nonexistent Traders”"
    )
    assert body["result"] is None and body["sql"] is None


async def test_a_partial_name_shared_by_customers_asks_which(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, sqlite_data_dir: Path
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    with _db(sqlite_data_dir) as db:
        # An adjective several demo customers share ("Global Foods 007", "Global Tools 012").
        word, n = db.execute(
            "SELECT substr(customer_name, 1, instr(customer_name, ' ') - 1) AS w, count(*) "
            "FROM analytics_customers WHERE tenant_code = 'acme' GROUP BY w "
            "HAVING count(*) > 1 ORDER BY count(*) DESC, w LIMIT 1"
        ).fetchone()
    assert n > 1
    body = (await ask(client, viewer, cid, f"invoice analysis for {word}")).json()
    assert body["status"] == "clarification", body
    assert "Did you mean" in body["clarification_question"]
    assert body["clarification_question"].count("#") >= 2  # candidates listed with their ids


async def test_a_named_customer_filters_a_planned_question(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str, sqlite_data_dir: Path
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    name, expected = _customer_with_invoices(sqlite_data_dir)
    question = f"show invoice count by status for {name}"
    body = (await ask(client, viewer, cid, question)).json()
    assert body["status"] == "succeeded", body
    analysis = body["analysis"]
    assert analysis["entity"]["resolved"] is True
    assert analysis["validation"]["entity_filter_applied"] is True
    assert any(f["field"] == "customer_id" for f in body["applied_filters"]["filters"])
    status = next(c for c in body["result"]["columns"] if c["name"] == "status")
    assert status["kind"] == "dimension"
    assert body["result"]["columns"][-1]["name"] == "invoice_count"
    counts = [r[-1] for r in body["result"]["rows"]]
    assert sum(counts) == expected  # every invoice of the customer, nobody else's
