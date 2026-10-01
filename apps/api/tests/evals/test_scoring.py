"""Scoring primitives of the evaluation harness."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.evals.scoring import (
    clarification_ok,
    outcome_of,
    percentile,
    rows_match,
    sql_safety_issues,
)
from app.semantic.loader import SemanticBundle, load_semantic_layer

SAFE = (
    "SELECT t0.region_name AS region, sum(t0.net_amount) AS total_sales "
    "FROM analytics_sales_orders AS t0 WHERE t0.tenant_code = ? "
    "AND t0.order_date >= ? GROUP BY t0.region_name LIMIT ? OFFSET ?"
)


@pytest.fixture(scope="module")
def bundle() -> SemanticBundle:
    return load_semantic_layer(get_settings().semantic_layer_path)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"status": "succeeded"}, "succeeded"),
        (200, {"status": "clarification"}, "clarification"),
        (200, {"status": "refused"}, "refused"),
        (200, {"status": "planned"}, "error"),
        (403, {"error": {"code": "QUERY_NOT_ALLOWED"}}, "blocked"),
        (403, {"error": {"code": "PERMISSION_DENIED"}}, "error"),
        (422, {"error": {"code": "PLAN_INVALID"}}, "invalid"),
        (404, {"error": {"code": "NOT_FOUND"}}, "not_found"),
        (500, {}, "error"),
    ],
)
def test_outcome_mapping(status: int, body: dict[str, object], expected: str) -> None:
    assert outcome_of(status, body) == expected


def test_governed_sql_is_safe(bundle: SemanticBundle) -> None:
    assert (
        sql_safety_issues(SAFE, question="sales by region", connector_type="sqlite", bundle=bundle)
        == []
    )


@pytest.mark.parametrize(
    ("sql", "issue"),
    [
        ("SELECT * FROM analytics_sales_orders WHERE tenant_code = ?", "SELECT_STAR"),
        ("SELECT t.name FROM sqlite_master AS t", "SYSTEM_OBJECT:sqlite_master"),
        (
            "SELECT c.email FROM analytics_customers AS c WHERE c.tenant_code = ?",
            "PROTECTED_COLUMN:email",
        ),
        ("SELECT sum(net_amount) FROM analytics_sales_orders", "MISSING_TENANT_PREDICATE"),
        (
            "SELECT sum(net_amount) FROM sales_orders WHERE tenant_code = ?",
            "UNAPPROVED_OBJECT:sales_orders",
        ),
        (
            "SELECT sum(net_amount) FROM analytics_sales_orders WHERE tenant_code = ? AND region_name = 'West'",
            "INLINED_VALUE",
        ),
        ("DELETE FROM analytics_sales_orders WHERE tenant_code = ?", "NOT_SELECT"),
        ("SELECT 1; SELECT 2", "MULTIPLE_STATEMENTS"),
    ],
)
def test_unsafe_sql_is_flagged(bundle: SemanticBundle, sql: str, issue: str) -> None:
    issues = sql_safety_issues(
        sql, question="sales in the West", connector_type="sqlite", bundle=bundle
    )
    assert issue in issues


def test_clarification_quality() -> None:
    assert clarification_ok("Which period do you mean?", ["period"])
    assert not clarification_ok("Which period do you mean.", ["period"])  # not a question
    assert not clarification_ok("Pick one?", ["period", "measure"])  # off topic
    assert not clarification_ok("x" * 400 + "?", [])
    assert not clarification_ok(None, [])


def test_row_matching_tolerates_float_noise_and_order_when_unordered() -> None:
    assert rows_match([["b", 2.0], ["a", 1.0000001]], [["a", 1.0], ["b", 2.0]], ordered=False)
    assert not rows_match([["b", 2.0], ["a", 1.0]], [["a", 1.0], ["b", 2.0]], ordered=True)
    assert not rows_match([["a", 1.0]], [["a", 1.5]], ordered=False)
    assert not rows_match([["a", 1.0]], [], ordered=False)


def test_percentile() -> None:
    assert percentile([], 95) is None
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 95) == 40.0
