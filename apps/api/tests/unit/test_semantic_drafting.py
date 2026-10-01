"""Drafting semantic entities from discovered metadata (onboarding a live database).

The demo ERP's *raw* tables stand in for a customer's live schema: nothing about them
is described in advance, and some columns are sensitive (email, bank account, tax ID,
payment token).
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from app.connectors.common.types import CanonicalType, ObjectMetadata
from app.query.plans import QueryPlan
from app.query.validator import PlanContext, resolve_plan
from app.semantic.drafting import (
    DraftColumn,
    DraftSource,
    check_with_layer,
    draft_semantic_layer,
    pascal,
    snake,
    write_drafts,
)
from app.semantic.loader import load_semantic_layer
from app.semantic.validator import bind_to_catalog
from tests.conftest import DEMO_SEMANTIC_LAYER
from tests.unit.test_query_engine import Engine, engine

__all__ = ["engine"]

PRODUCT_LAYER = Path(__file__).resolve().parents[4] / "semantic-layer"
RAW_TABLES = {"customers", "sales_orders", "sales_order_lines", "products", "payments", "invoices"}


def raw_sources(catalog: list[ObjectMetadata]) -> list[DraftSource]:
    return [
        DraftSource.from_api(o.model_dump(mode="json")) for o in catalog if o.name in RAW_TABLES
    ]


def empty_layer(tmp_path: Path) -> Path:
    """A fresh customer layer: the product's skeleton (policy only, no entities)."""
    root = tmp_path / "semantic-layer"
    shutil.copytree(PRODUCT_LAYER, root)
    return root


def test_product_ships_an_empty_customer_owned_layer() -> None:
    bundle = load_semantic_layer(PRODUCT_LAYER)
    assert bundle.entities == {} and bundle.examples == []
    assert bundle.sensitive_patterns, "the sensitive-field policy must ship"
    assert DEMO_SEMANTIC_LAYER != PRODUCT_LAYER


async def test_drafts_from_live_metadata_load_bind_and_answer(
    engine: Engine, tmp_path: Path
) -> None:
    layer = empty_layer(tmp_path)
    policy = load_semantic_layer(layer).policy
    drafts = draft_semantic_layer(raw_sources(engine.catalog), policy)
    assert {d.entity for d in drafts.entities} == {
        "Customers",
        "SalesOrders",
        "SalesOrderLines",
        "Products",
        "Payments",
        "Invoices",
    }
    write_drafts(layer, drafts, "ERP production")
    bundle = load_semantic_layer(layer)
    bindings = bind_to_catalog(bundle, engine.catalog)
    assert set(bundle.entities) <= set(bindings)

    orders = bundle.entities["SalesOrders"]
    assert {d.name for d in orders.dimensions} >= {"order_id", "customer_id", "status"}
    assert orders.default_time_field == "order_date"
    assert orders.measures[0].name == "record_count"

    ctx = PlanContext(
        tenant_id=uuid.uuid4(),
        tenant_slug="acme",
        tenant_settings={},
        connection_id=uuid.uuid4(),
        semantic_version=1,
        permissions=frozenset({"analytics.query"}),
        today=engine.ctx().today,
        max_rows=1000,
    )
    plan = QueryPlan.model_validate(
        {
            "entity": "SalesOrders",
            "dimensions": ["status"],
            "measures": ["record_count"],
            "date_range": {"field": "order_date", "relative": "last_12_months"},
        }
    )
    resolved = resolve_plan(plan, bundle=bundle, bindings=bindings, catalog=engine.catalog, ctx=ctx)
    compiled = engine.adapter.compile_query_plan(resolved)
    result = await engine.adapter.execute_readonly(compiled.statement)
    assert result.row_count > 0


def test_sensitive_columns_are_never_exposed(engine: Engine, tmp_path: Path) -> None:
    policy = load_semantic_layer(empty_layer(tmp_path)).policy
    drafts = {
        d.entity: d for d in draft_semantic_layer(raw_sources(engine.catalog), policy).entities
    }
    customers = drafts["Customers"].document
    exposed = {
        f["field"] for key in ("dimensions", "measures", "time_fields") for f in customers[key]
    }
    assert {"email", "phone", "bank_account", "tax_id"} <= set(
        customers["security"]["forbidden_fields"]
    )
    assert not exposed & {"email", "phone", "bank_account", "tax_id"}
    payments = drafts["Payments"].document
    assert "payment_token" in payments["security"]["forbidden_fields"]


def test_foreign_keys_become_join_drafts(engine: Engine, tmp_path: Path) -> None:
    policy = load_semantic_layer(empty_layer(tmp_path)).policy
    joins = draft_semantic_layer(raw_sources(engine.catalog), policy).joins
    pairs = {(j["from"], j["to"]) for j in joins}
    assert ("SalesOrderLines", "SalesOrders") in pairs
    assert ("SalesOrders", "Customers") in pairs
    assert all(j["relationship"] == "many_to_one" for j in joins)


def test_column_rules() -> None:
    source = DraftSource(
        schema_name="dbo",
        name="tblOrderHeader",
        kind="table",
        row_estimate=5_000_000,
        primary_key=["OrderID"],
        columns=[
            DraftColumn("OrderID", CanonicalType.INTEGER),
            DraftColumn("CustomerNo", CanonicalType.INTEGER),
            DraftColumn("Quantity", CanonicalType.INTEGER),
            DraftColumn("NetAmount", CanonicalType.DECIMAL, "Net amount in EUR"),
            DraftColumn("paid", CanonicalType.DECIMAL),
            DraftColumn("PostingDate", CanonicalType.DATE),
            DraftColumn("CreatedAt", CanonicalType.TIMESTAMP),
            DraftColumn("Attributes", CanonicalType.JSON),
            DraftColumn("Order Note", CanonicalType.STRING),
            DraftColumn("ContactEmail", CanonicalType.STRING),
        ],
    )
    from app.semantic.policy import SensitiveFieldPolicy

    policy = load_semantic_layer(PRODUCT_LAYER).policy
    assert isinstance(policy, SensitiveFieldPolicy)
    draft = draft_semantic_layer([source], policy).entities[0]
    doc = draft.document
    assert draft.entity == "TblOrderHeader"
    dims = {d["field"] for d in doc["dimensions"]}
    measures = {m["field"]: m for m in doc["measures"]}
    assert {"OrderID", "CustomerNo"} <= dims
    assert measures["Quantity"]["format"] == "integer"
    assert measures["NetAmount"]["format"] == "currency"
    assert measures["NetAmount"]["description"] == "Net amount in EUR"
    assert "paid" in measures  # 'paid' ends in "id" but is not a key
    assert measures["OrderID"]["aggregation"] == "count"  # record_count
    assert doc["default_time_field"] == "posting_date" and doc["large_fact"] is True
    assert doc["source"] == {
        "logical_name": "tblOrderHeader",
        "allowed_database_objects": ["tblOrderHeader"],
        "schema_name": "dbo",
    }
    assert "ContactEmail" in doc["security"]["forbidden_fields"]
    assert any("Attributes" in s for s in draft.skipped)
    assert any("Order Note" in s for s in draft.skipped)


def test_names_are_valid_identifiers() -> None:
    assert pascal("sales_order_header") == "SalesOrderHeader"
    assert pascal("2023_orders") == "T2023Orders"
    assert snake("CustomerID") == "customer_id"
    assert snake("NetAmountEUR") == "net_amount_eur"
    assert snake("1st_level") == "f_1st_level"


def test_existing_files_are_not_overwritten(engine: Engine, tmp_path: Path) -> None:
    layer = empty_layer(tmp_path)
    policy = load_semantic_layer(layer).policy
    drafts = draft_semantic_layer(raw_sources(engine.catalog), policy)
    write_drafts(layer, drafts, "ERP")
    with pytest.raises(FileExistsError):
        write_drafts(layer, drafts, "ERP")
    assert write_drafts(layer, drafts, "ERP", overwrite=True)


def test_drafts_are_checked_together_with_the_existing_layer(
    engine: Engine, tmp_path: Path
) -> None:
    layer = empty_layer(tmp_path)
    policy = load_semantic_layer(layer).policy
    drafts = draft_semantic_layer(raw_sources(engine.catalog), policy)
    bundle = check_with_layer(layer, drafts, "ERP")
    assert len(bundle.entities) == len(drafts.entities)
    assert not list((layer / "entities").glob("*.yaml"))  # checking writes nothing


def test_rates_and_unit_prices_average_instead_of_summing() -> None:
    from app.semantic.policy import SensitiveFieldPolicy

    source = DraftSource(
        schema_name="public",
        name="trade_shipments",
        kind="table",
        row_estimate=10,
        primary_key=["id"],
        columns=[
            DraftColumn("id", CanonicalType.INTEGER),
            DraftColumn("value_usd", CanonicalType.DECIMAL),
            DraftColumn("total_price", CanonicalType.DECIMAL),
            DraftColumn("unit_price_usd", CanonicalType.DECIMAL),
            DraftColumn("TaxRate", CanonicalType.DECIMAL),
            DraftColumn("discount_pct", CanonicalType.DECIMAL),
            DraftColumn("generated_units", CanonicalType.DECIMAL),
        ],
    )
    policy = load_semantic_layer(PRODUCT_LAYER).policy
    assert isinstance(policy, SensitiveFieldPolicy)
    measures = {
        m["field"]: m
        for m in draft_semantic_layer([source], policy).entities[0].document["measures"]
    }
    assert {f: m["aggregation"] for f, m in measures.items() if f != "id"} == {
        "value_usd": "sum",
        "total_price": "sum",
        "unit_price_usd": "avg",
        "TaxRate": "avg",
        "discount_pct": "avg",
        "generated_units": "sum",  # contains "rate" only inside a word
    }
    assert measures["unit_price_usd"]["description"] == "Average of unit_price_usd."
