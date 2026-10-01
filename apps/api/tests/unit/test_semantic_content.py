"""The shipped semantic layer + demo data: loads cleanly, binds to the demo views,
is deterministic and never exposes sensitive columns."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import column, func, select, table

from app.connectors.base import DatabaseAdapter
from app.connectors.common.types import CatalogSnapshot, ConnectionConfig, ConnectorType
from app.connectors.factory import ConnectorFactory
from app.semantic.loader import SemanticBundle, load_semantic_layer
from app.semantic.validator import bind_to_catalog
from tests.conftest import make_settings
from tests.fixtures.demo_erp import VIEW_NAMES, seed_demo

REPO_SEMANTIC_LAYER = Path(__file__).resolve().parents[1] / "fixtures" / "semantic-layer"
ANCHOR = date(2026, 9, 23)


@pytest.fixture(scope="module")
def bundle() -> SemanticBundle:
    return load_semantic_layer(REPO_SEMANTIC_LAYER)


async def _seed(data_dir: Path, name: str = "demo.db") -> Path:
    path = data_dir / name
    await seed_demo(f"sqlite+aiosqlite:///{path.as_posix()}", anchor_date=ANCHOR)
    return path


@pytest.fixture
async def demo_adapter(tmp_path: Path) -> AsyncIterator[DatabaseAdapter]:
    data_dir = tmp_path / "sqlite-data"
    data_dir.mkdir()
    await _seed(data_dir)
    settings = make_settings(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}", data_dir)
    adapter = ConnectorFactory(settings).create(
        ConnectionConfig(
            connection_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            connector_type=ConnectorType.SQLITE,
            host=None,
            port=None,
            database="demo.db",
            schema=None,
            username=None,
            password=None,
            max_rows=1000,
        )
    )
    try:
        yield adapter
    finally:
        await adapter.close()


# ---------------------------------------------------------------------- layer
def test_repo_semantic_layer_loads_without_warnings(bundle: SemanticBundle) -> None:
    assert bundle.warnings == ()
    assert {
        "SalesOrder",
        "SalesLine",
        "Inventory",
        "InventoryMovement",
        "Customer",
        "Invoice",
        "PurchaseOrder",
    } <= set(bundle.entities)
    assert len(bundle.examples) >= 35
    assert len(bundle.terms) >= 25
    assert {a.abbreviation for a in bundle.abbreviations} >= {
        "SO",
        "PO",
        "SKU",
        "AOV",
        "YTD",
        "MTD",
    }


def test_every_entity_is_tenant_filtered(bundle: SemanticBundle) -> None:
    for entity in bundle.entities.values():
        assert [(f.field, f.value_from) for f in entity.security.required_filters] == [
            ("tenant_code", "tenant.slug")
        ], entity.entity


def test_spec_questions_have_examples(bundle: SemanticBundle) -> None:
    questions = {e.question.lower() for e in bundle.examples}
    for fragment in (
        "total sales by region for the last six months",
        "highest sales last month",
        "open sales orders older than seven days",
        "below minimum stock",
        "this month's sales with last month",
        "customers with overdue payments",
        "average order value by branch",
        "sales decreased in the west",
        "inventory movement for product a",
    ):
        assert any(fragment in q for q in questions), fragment


def test_sensitive_requests_are_marked_for_refusal(bundle: SemanticBundle) -> None:
    refusals = [e for e in bundle.examples if e.plan.get("refuse")]
    assert len(refusals) >= 3
    assert all(e.intent == "clarification" and e.notes and "REFUSED" in e.notes for e in refusals)


# ---------------------------------------------------------------- demo + bind
async def test_every_entity_binds_to_the_demo_views(
    bundle: SemanticBundle, demo_adapter: DatabaseAdapter
) -> None:
    catalog = await demo_adapter.get_catalog(["main"])
    bindings = bind_to_catalog(bundle, catalog.objects, default_schema="main")
    problems = {name: b.problems for name, b in bindings.items() if not b.bound}
    assert problems == {}
    assert {b.object_name for b in bindings.values()} <= set(VIEW_NAMES)


async def test_join_keys_exist_on_both_sides(
    bundle: SemanticBundle, demo_adapter: DatabaseAdapter
) -> None:
    catalog: CatalogSnapshot = await demo_adapter.get_catalog(["main"])
    columns = {o.name: {c.name for c in o.columns} for o in catalog.objects}
    for join in bundle.joins:
        left = bundle.entities[join.from_entity].source.logical_name
        right = bundle.entities[join.to_entity].source.logical_name
        for key in join.on:
            assert key.from_field in columns[left], (join.name, key.from_field)
            assert key.to_field in columns[right], (join.name, key.to_field)


async def test_views_expose_no_sensitive_columns(
    bundle: SemanticBundle, demo_adapter: DatabaseAdapter
) -> None:
    catalog = await demo_adapter.get_catalog(["main"])
    policy = bundle.policy
    views = [o for o in catalog.objects if o.name in VIEW_NAMES]
    assert len(views) == len(VIEW_NAMES)
    leaked = {(v.name, c.name) for v in views for c in v.columns if policy.classify(c.name)}
    assert leaked == set()
    # ...while the raw tables really do contain them (so the test is meaningful).
    raw_customers = next(o for o in catalog.objects if o.name == "customers")
    assert {"bank_account", "tax_id", "email"} <= {c.name for c in raw_customers.columns}


async def _scalar(adapter: DatabaseAdapter, query: Any) -> Any:
    result = await adapter.execute_readonly(query)
    return result.rows[0][0]


async def test_demo_storylines_have_data(demo_adapter: DatabaseAdapter) -> None:
    orders = table(
        "analytics_sales_orders",
        column("order_date"),
        column("status"),
        column("days_open"),
        column("net_amount"),
        column("region_name"),
    )
    inventory = table("analytics_inventory", column("below_min"))
    invoices = table("analytics_invoices", column("is_overdue"), column("days_overdue"))
    moves = table("analytics_inventory_movements", column("product_name"), column("movement_date"))

    last_6m = await _scalar(
        demo_adapter,
        select(func.count()).select_from(orders).where(orders.c.order_date >= date(2026, 3, 23)),
    )
    stale_open = await _scalar(
        demo_adapter,
        select(func.count())
        .select_from(orders)
        .where(orders.c.status.in_(["open", "processing"]), orders.c.days_open > 7),
    )
    below_min = await _scalar(demo_adapter, select(func.sum(inventory.c.below_min)))
    overdue = await _scalar(demo_adapter, select(func.sum(invoices.c.is_overdue)))
    product_a_qtr = await _scalar(
        demo_adapter,
        select(func.count())
        .select_from(moves)
        .where(moves.c.product_name == "Product A", moves.c.movement_date >= date(2026, 7, 1)),
    )
    assert last_6m > 100
    assert stale_open > 0
    assert below_min > 0
    assert overdue > 0
    assert product_a_qtr >= 20

    def region_sales(start: date, end: date) -> Any:
        return (
            select(orders.c.region_name, func.sum(orders.c.net_amount))
            .where(orders.c.order_date >= start, orders.c.order_date < end)
            .group_by(orders.c.region_name)
        )

    recent = dict(
        (
            await demo_adapter.execute_readonly(region_sales(date(2026, 6, 25), date(2026, 9, 24)))
        ).rows
    )
    before = dict(
        (
            await demo_adapter.execute_readonly(region_sales(date(2026, 3, 27), date(2026, 6, 25)))
        ).rows
    )
    # Storyline: West declines relative to its previous quarter, more than others.
    west_change = float(recent["West"]) / float(before["West"])
    others = [float(recent[r]) / float(before[r]) for r in ("North", "South", "East")]
    assert west_change < min(others)
    assert west_change < 0.8


async def test_seed_is_deterministic(tmp_path: Path) -> None:
    import sqlite3

    def fingerprint(path: Path) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(path)
        try:
            return [
                conn.execute(f"SELECT COUNT(*), ROUND(SUM({col}), 2) FROM {view}").fetchone()
                for view, col in (
                    ("analytics_sales_orders", "net_amount"),
                    ("analytics_sales_lines", "quantity"),
                    ("analytics_inventory", "stock_value"),
                    ("analytics_invoices", "outstanding_amount"),
                    ("analytics_purchase_orders", "total_amount"),
                    ("analytics_inventory_movements", "quantity"),
                )
            ]
        finally:
            conn.close()

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = await _seed(tmp_path / "a")
    second = await _seed(tmp_path / "b")
    assert fingerprint(first) == fingerprint(second)


async def test_seed_refuses_to_overwrite_and_validates_input(tmp_path: Path) -> None:
    path = await _seed(tmp_path)
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    with pytest.raises(RuntimeError, match="already present"):
        await seed_demo(url, anchor_date=ANCHOR)
    report = await seed_demo(url, anchor_date=ANCHOR, reset=True)
    assert report.row_counts["sales_orders"] > 0
    with pytest.raises(ValueError, match="tenant_code"):
        await seed_demo(url, anchor_date=ANCHOR, tenant_code="Bad Code; DROP", reset=True)
