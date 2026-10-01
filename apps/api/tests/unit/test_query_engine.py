"""Phase 4 query engine: plan model, periods, plan validation, SQL compilation,
independent SQL validation of compiled output, execution correctness against the
demo ERP, result formatting and the deterministic mock planner."""

from __future__ import annotations

import dataclasses
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy_hana.dialect import HANAHDBCLIDialect

from app.config import get_settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import UnsupportedConnectorError
from app.connectors.common.types import ConnectionConfig, ConnectorType, ObjectMetadata
from app.connectors.factory import ConnectorFactory
from app.llm.mock_planner import explain_result, parse_period, plan_query
from app.query.compiler import CompiledQuery, compile_plan
from app.query.normalizer import Window, plan_hash, resolve_period
from app.query.plans import DateRange, QueryPlan, RelativePeriod
from app.query.result_formatter import format_result
from app.query.sql_validator import SqlPolicy, SqlSafetyError, validate_sql
from app.query.validator import (
    IssueCode,
    PlanContext,
    PlanValidationError,
    ResolvedPlan,
    resolve_plan,
)
from app.semantic.loader import SemanticBundle, load_semantic_layer
from app.semantic.models import TimeGrain
from app.semantic.validator import EntityBinding, bind_to_catalog
from tests.conftest import make_settings
from tests.fixtures.demo_erp import clone_tenant_sqlite, seed_demo

TODAY = date(2026, 9, 15)
TENANT = "acme"
SQLITE: Dialect = sqlite.dialect()
POSTGRES: Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
MYSQL: Dialect = mysql.dialect()
MSSQL: Dialect = mssql.dialect()  # type: ignore[no-untyped-call]
ORACLE: Dialect = oracle.dialect()  # type: ignore[no-untyped-call]
HANA: Dialect = HANAHDBCLIDialect()
ALL_DIALECTS: tuple[tuple[str, Dialect], ...] = (
    ("sqlite", SQLITE),
    ("postgresql", POSTGRES),
    ("mysql", MYSQL),
    ("mssql", MSSQL),
    ("oracle", ORACLE),
    ("hana", HANA),
)


# ------------------------------------------------------------------ fixtures
@dataclass
class Engine:
    bundle: SemanticBundle
    catalog: list[ObjectMetadata]
    bindings: dict[str, EntityBinding]
    adapter: DatabaseAdapter
    db_path: Path

    def ctx(self, **overrides: Any) -> PlanContext:
        values: dict[str, Any] = {
            "tenant_id": uuid.uuid4(),
            "tenant_slug": TENANT,
            "tenant_settings": {},
            "connection_id": uuid.uuid4(),
            "semantic_version": 1,
            "permissions": frozenset({"analytics.query"}),
            "today": TODAY,
            "max_rows": 1000,
        }
        values.update(overrides)
        return PlanContext(**values)

    def resolve(self, plan: dict[str, Any] | QueryPlan, **ctx: Any) -> ResolvedPlan:
        qp = plan if isinstance(plan, QueryPlan) else QueryPlan.model_validate(plan)
        return resolve_plan(
            qp,
            bundle=self.bundle,
            bindings=self.bindings,
            catalog=self.catalog,
            ctx=self.ctx(**ctx),
        )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Engine]:
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "demo.db"
    await seed_demo(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", anchor_date=TODAY, tenant_code=TENANT
    )
    clone_tenant(db_path, "globex")  # a second tenant's rows that must never leak
    settings = make_settings(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}", data)
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
    catalog = (await adapter.get_catalog()).objects
    bundle = load_semantic_layer(get_settings().semantic_layer_path)
    yield Engine(bundle, catalog, bind_to_catalog(bundle, catalog), adapter, db_path)
    await adapter.close()


def clone_tenant(db: Path, tenant_code: str) -> None:
    """Copy every demo row to another tenant (ids offset) inside the same database."""
    clone_tenant_sqlite(str(db), tenant_code)


def sql_policy(engine: Engine, resolved: ResolvedPlan, dialect: str) -> SqlPolicy:
    tables: set[str] = set()
    columns: set[str] = set()
    for source in resolved.sources:
        entity = engine.bundle.entities[source.entity]
        tables |= {o.lower() for o in entity.source.allowed_database_objects}
        columns |= {f.lower() for f in entity.physical_fields()}
    return SqlPolicy(
        dialect=dialect,
        allowed_tables=frozenset(tables),
        allowed_columns=frozenset(columns),
        required_equality_columns=frozenset(rf.field for rf in resolved.row_filters),
        max_limit=1001,
    )


def data_examples(bundle: SemanticBundle) -> list[tuple[str, QueryPlan]]:
    plans = [(e.id, QueryPlan.model_validate(e.plan)) for e in bundle.examples]
    return [(i, p) for i, p in plans if p.is_data_plan]


def direct(db: Path, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


# ------------------------------------------------------------------ plan model
def test_all_semantic_examples_are_valid_query_plans() -> None:
    bundle = load_semantic_layer(get_settings().semantic_layer_path)
    assert len(bundle.examples) >= 30
    for example in bundle.examples:
        QueryPlan.model_validate(example.plan)


@pytest.mark.parametrize(
    "bad",
    [
        {"entity": "SalesOrder", "measures": ["total_sales"], "sql": "SELECT 1"},  # extra key
        {"entity": "SalesOrder", "measures": ["total_sales"], "limit": 0},
        {"entity": "SalesOrder", "measures": ["total_sales"], "limit": 100_000},
        {"entity": "Sales Order; DROP", "measures": ["total_sales"]},
        {"entity": "SalesOrder", "measures": ["x; DROP TABLE t"]},
        {"entity": "SalesOrder", "date_range": {"field": "order_date", "relative": "last_n_days"}},
    ],
)
def test_query_plan_rejects_malformed_model_output(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(bad)


@pytest.mark.parametrize(
    ("relative", "n", "start", "end"),
    [
        (RelativePeriod.TODAY, None, date(2026, 9, 15), date(2026, 9, 16)),
        (RelativePeriod.YESTERDAY, None, date(2026, 9, 14), date(2026, 9, 15)),
        (RelativePeriod.LAST_7_DAYS, None, date(2026, 9, 9), date(2026, 9, 16)),
        (RelativePeriod.THIS_MONTH, None, date(2026, 9, 1), date(2026, 9, 16)),
        (RelativePeriod.LAST_MONTH, None, date(2026, 8, 1), date(2026, 9, 1)),
        (RelativePeriod.THIS_QUARTER, None, date(2026, 7, 1), date(2026, 9, 16)),
        (RelativePeriod.LAST_QUARTER, None, date(2026, 4, 1), date(2026, 7, 1)),
        (RelativePeriod.QUARTER_BEFORE_LAST, None, date(2026, 1, 1), date(2026, 4, 1)),
        (RelativePeriod.YEAR_TO_DATE, None, date(2026, 1, 1), date(2026, 9, 16)),
        (RelativePeriod.LAST_YEAR, None, date(2025, 1, 1), date(2026, 1, 1)),
        (RelativePeriod.LAST_N_DAYS, 3, date(2026, 9, 13), date(2026, 9, 16)),
    ],
)
def test_relative_periods_resolve_to_half_open_windows(
    relative: RelativePeriod, n: int | None, start: date, end: date
) -> None:
    window = resolve_period(DateRange(field="order_date", relative=relative, n=n), TODAY)
    assert window == Window(start, end)


def test_plan_hash_is_stable_and_scope_sensitive() -> None:
    plan = QueryPlan.model_validate({"entity": "SalesOrder", "measures": ["total_sales"]})
    same = QueryPlan.model_validate({"measures": ["total_sales"], "entity": "SalesOrder"})
    w = {"current": Window(date(2026, 1, 1), date(2026, 2, 1))}
    scope = {"tenant": "t1", "connection": "c1", "version": "1"}
    assert plan_hash(plan, w, scope) == plan_hash(same, w, dict(reversed(scope.items())))
    assert plan_hash(plan, w, scope) != plan_hash(plan, w, {**scope, "tenant": "t2"})
    later = {"current": Window(date(2026, 2, 1), date(2026, 3, 1))}
    assert plan_hash(plan, w, scope) != plan_hash(plan, later, scope)


# ------------------------------------------------------------------ validation
async def test_resolution_adds_row_filter_default_period_and_clamps_limit(engine: Engine) -> None:
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["region"],
            "limit": 900,
        },
        max_rows=50,
    )
    assert [(rf.field, rf.value) for rf in resolved.row_filters] == [("tenant_code", TENANT)]
    assert resolved.limit == 50
    assert resolved.time_filter is not None  # large fact table: default period applied
    assert any("last 90 days" in a.lower() or "90" in a for a in resolved.assumptions)
    assert any("50" in a for a in resolved.assumptions)


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        ({"entity": "Nope", "measures": ["total_sales"]}, IssueCode.UNKNOWN_ENTITY),
        ({"entity": "SalesOrder", "measures": ["profit_xyz"]}, IssueCode.UNKNOWN_FIELD),
        (
            {
                "entity": "SalesOrder",
                "measures": ["total_sales"],
                "dimensions": ["customer_bank_account"],
            },
            IssueCode.FORBIDDEN_FIELD,
        ),
        (
            {
                "entity": "SalesOrder",
                "measures": ["total_sales"],
                "filters": [{"field": "total_sales", "op": "gt", "value": 5}],
            },
            IssueCode.UNKNOWN_FIELD,
        ),
    ],
)
async def test_invalid_plans_are_rejected_with_codes(
    engine: Engine, plan: dict[str, Any], code: IssueCode
) -> None:
    with pytest.raises(PlanValidationError) as info:
        engine.resolve(plan)
    codes = {i.code for i in info.value.issues}
    assert code in codes or (code is IssueCode.UNKNOWN_FIELD and codes), codes


async def test_forbidden_fields_are_not_retryable(engine: Engine) -> None:
    with pytest.raises(PlanValidationError) as info:
        engine.resolve(
            {
                "entity": "SalesOrder",
                "measures": ["total_sales"],
                "dimensions": ["customer_bank_account"],
            }
        )
    assert not info.value.retryable
    assert info.value.status_code == 403


async def test_missing_permission_blocks_the_entity(engine: Engine) -> None:
    with pytest.raises(PlanValidationError) as info:
        engine.resolve(
            {"entity": "SalesOrder", "measures": ["total_sales"]}, permissions=frozenset()
        )
    assert IssueCode.ENTITY_NOT_PERMITTED in {i.code for i in info.value.issues}


# ------------------------------------------------------------------ compilation
async def test_every_example_compiles_validates_and_executes(engine: Engine) -> None:
    examples = data_examples(engine.bundle)
    assert len(examples) >= 30
    for example_id, plan in examples:
        resolved = engine.resolve(plan)
        for dialect_name, dialect in ALL_DIALECTS:
            if dialect_name == "hana" and any(
                d.time_grain in {TimeGrain.WEEK, TimeGrain.QUARTER} for d in resolved.dimensions
            ):
                continue  # SAP HANA foundation: week/quarter buckets not available yet
            compiled = compile_plan(resolved, dialect_name)
            policy = sql_policy(engine, resolved, dialect_name)
            validate_sql(compiled.render(dialect), policy)
            if compiled.previous_statement is not None:
                validate_sql(compiled.render(dialect, previous=True), policy)
            sql = compiled.render(dialect)
            assert "SELECT *" not in sql and ".*" not in sql, example_id
            assert "tenant_code" in sql, example_id
            assert TENANT not in sql, example_id  # values stay bound
        compiled = compile_plan(resolved, "sqlite")
        result = await engine.adapter.execute_readonly(compiled.statement, max_rows=resolved.limit)
        assert result.row_count <= resolved.limit, example_id


async def _run(engine: Engine, plan: dict[str, Any]) -> tuple[ResolvedPlan, CompiledQuery, Any]:
    resolved = engine.resolve(plan)
    compiled = compile_plan(resolved, "sqlite")
    return resolved, compiled, await engine.adapter.execute_readonly(compiled.statement)


async def test_region_totals_match_direct_sql_for_the_tenant_only(engine: Engine) -> None:
    resolved, _, result = await _run(
        engine,
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["region"],
            "date_range": {"field": "order_date", "relative": "last_6_months"},
        },
    )
    tf = resolved.time_filter
    assert tf is not None
    expected = direct(
        engine.db_path,
        "SELECT region_name, round(sum(net_amount), 2) FROM analytics_sales_orders "
        "WHERE tenant_code = ? AND order_date >= ? AND order_date < ? GROUP BY region_name",
        TENANT,
        tf.window.start.isoformat(),
        tf.window.end.isoformat(),
    )
    got = {row[0]: round(float(row[1]), 2) for row in result.rows}
    assert got == {name: pytest.approx(total) for name, total in expected}
    everyone = direct(
        engine.db_path,
        "SELECT round(sum(net_amount), 2) FROM analytics_sales_orders "
        "WHERE order_date >= ? AND order_date < ?",
        tf.window.start.isoformat(),
        tf.window.end.isoformat(),
    )[0][0]
    assert sum(got.values()) < everyone  # the other tenant's rows were excluded


async def test_filter_operators_compile_to_bound_parameters(engine: Engine) -> None:
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["order_count"],
            "dimensions": ["region"],
            "filters": [
                {"field": "region", "op": "in", "value": ["North", "South"]},
                {"field": "customer", "op": "contains", "value": "50%_off"},
                {"field": "status", "op": "ne", "value": "cancelled"},
            ],
            "date_range": {"field": "order_date", "relative": "last_12_months"},
        }
    )
    compiled = compile_plan(resolved, "postgresql")
    sql = compiled.render(POSTGRES)
    assert "North" not in sql and "50%_off" not in sql and "cancelled" not in sql
    params = compiled.parameters(POSTGRES)
    assert ["North", "South"] in params.values()
    assert any(isinstance(v, str) and "/%" in v for v in params.values())  # LIKE autoescape
    result = await engine.adapter.execute_readonly(compile_plan(resolved, "sqlite").statement)
    assert {r[0] for r in result.rows} <= {"North", "South"}


async def test_explain_path_binds_dates_and_expanding_parameters(engine: Engine) -> None:
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "filters": [{"field": "region", "op": "in", "value": ["North", "West"]}],
            "date_range": {"field": "order_date", "relative": "last_month"},
        }
    )
    explain = await engine.adapter.explain_query(compile_plan(resolved, "sqlite").statement)
    assert explain.plan


async def test_quarter_trend_on_sqlite(engine: Engine) -> None:
    _, compiled, result = await _run(
        engine,
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["order_date"],
            "time_grain": "quarter",
            "date_range": {"field": "order_date", "relative": "this_year"},
        },
    )
    # Quarter buckets are the quarter's first day, matching PostgreSQL date_trunc('quarter').
    assert [r[0] for r in result.rows] == ["2026-01-01", "2026-04-01", "2026-07-01"]
    sql = compiled.render(SQLITE)
    assert "printf" in sql
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["order_date"],
            "time_grain": "quarter",
            "date_range": {"field": "order_date", "relative": "this_year"},
        }
    )
    for name, dialect in (("sqlite", SQLITE), ("postgresql", POSTGRES)):
        validate_sql(
            compile_plan(resolved, name).render(dialect), sql_policy(engine, resolved, name)
        )


async def test_sql_validator_rejects_tampered_compiled_sql(engine: Engine) -> None:
    resolved = engine.resolve({"entity": "SalesOrder", "measures": ["total_sales"]})
    sql = compile_plan(resolved, "sqlite").render(SQLITE)
    policy = sql_policy(engine, resolved, "sqlite")
    for tampered in (
        sql.replace("t0.tenant_code = ? AND ", ""),  # row filter removed
        sql + "; DROP TABLE analytics_sales_orders",
        sql.replace("analytics_sales_orders", "users"),
        sql.replace("sum(t0.net_amount)", "sum(t0.bank_account)"),
    ):
        with pytest.raises(SqlSafetyError):
            validate_sql(tampered, policy)


# ------------------------------------------------------------------ formatting
async def test_comparison_result_has_change_columns_and_kpi(engine: Engine) -> None:
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "comparison": {
                "current": {"field": "order_date", "relative": "this_month"},
                "previous": {"field": "order_date", "relative": "last_month"},
            },
        }
    )
    compiled = compile_plan(resolved, "sqlite")
    assert compiled.previous_statement is not None
    current = await engine.adapter.execute_readonly(compiled.statement)
    previous = await engine.adapter.execute_readonly(compiled.previous_statement)
    formatted = format_result(resolved, compiled.output, current, previous, engine.bundle.policy)
    names = [c.name for c in formatted.columns]
    assert names == [
        "total_sales",
        "total_sales_previous",
        "total_sales_change",
        "total_sales_change_pct",
    ]
    kpi = formatted.kpis[0]
    assert kpi.previous is not None and kpi.change_pct is not None
    assert formatted.chart.type == "kpi"


async def test_grouped_result_chart_and_totals(engine: Engine) -> None:
    resolved, compiled, result = await _run(
        engine,
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["region"],
            "date_range": {"field": "order_date", "relative": "this_year"},
        },
    )
    formatted = format_result(resolved, compiled.output, result, None, engine.bundle.policy)
    assert formatted.chart.type == "bar" and formatted.chart.x == "region"
    assert formatted.kpis[0].scope == "total"
    assert formatted.kpis[0].label == "Total sales"
    assert formatted.row_count == len(formatted.rows) == 4


# ------------------------------------------------------------------ mock planner
def test_mock_period_parser() -> None:
    assert parse_period("sales for the last six months") == {"relative": "last_6_months"}
    assert parse_period("revenue last quarter") == {"relative": "last_quarter"}
    assert parse_period("orders in the last 45 days") == {"relative": "last_n_days", "n": 45}


def test_mock_planner_refuses_sensitive_requests() -> None:
    content = "<<<QUESTION>>>\nlist customer bank account numbers\n<<<END QUESTION>>>"
    plan = plan_query([{"role": "system", "content": ""}, {"role": "user", "content": content}])
    assert plan["refuse"] is True


def test_mock_explanation_counts_hidden_rows() -> None:
    data = "<<<DATA>>>\nregion | total_sales\nNorth | 10\nSouth | 5\n(3 more rows not shown)\n<<<END DATA>>>"
    out = explain_result([{"role": "user", "content": data}])
    assert out.summary.startswith("5 row(s)")
    assert "North" in out.highlights[0]


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
@pytest.mark.parametrize(("dialect_name", "dialect"), ALL_DIALECTS)
async def test_time_buckets_validate_on_every_dialect(
    engine: Engine, grain: str, dialect_name: str, dialect: Dialect
) -> None:
    resolved = engine.resolve(
        {
            "entity": "SalesOrder",
            "measures": ["total_sales"],
            "dimensions": ["order_date"],
            "time_grain": grain,
            "date_range": {"field": "order_date", "relative": "this_year"},
        }
    )
    if dialect_name == "hana" and grain in {"week", "quarter"}:
        with pytest.raises(UnsupportedConnectorError):
            compile_plan(resolved, dialect_name)
        return
    sql = compile_plan(resolved, dialect_name).render(dialect)
    validated = validate_sql(sql, sql_policy(engine, resolved, dialect_name))
    # Constants in the bucket are literals, so GROUP BY repeats the SELECT expression
    # exactly (SQL Server rejects bound parameters there).
    group_by = re.sub(r"'[^']*'", "''", sql.split("GROUP BY", 1)[1].split("ORDER BY", 1)[0])
    assert not any(marker in group_by for marker in ("?", "%s", ":"))
    assert validated.has_limit


def test_sqlserver_day_bucket_truncates_to_date() -> None:
    import sqlalchemy as sa

    from app.query.compiler import time_bucket

    expr = time_bucket("mssql", sa.column("order_date"), TimeGrain.DAY)
    assert str(expr.compile(dialect=MSSQL)) == "CAST(order_date AS DATE)"


def test_top_and_fetch_limits_are_enforced() -> None:
    policy = SqlPolicy(
        dialect="mssql",
        allowed_tables=frozenset({"t"}),
        allowed_columns=frozenset({"a", "tenant_code"}),
        required_equality_columns=frozenset({"tenant_code"}),
        max_limit=100,
    )
    ok = "SELECT TOP 100 t0.a FROM t AS t0 WHERE t0.tenant_code = ?"
    assert validate_sql(ok, policy).has_limit
    with pytest.raises(SqlSafetyError):
        validate_sql("SELECT TOP 5000 t0.a FROM t AS t0 WHERE t0.tenant_code = ?", policy)
    with pytest.raises(SqlSafetyError):
        validate_sql("SELECT t0.a FROM t AS t0 WHERE t0.tenant_code = ?", policy)
    oracle_policy = dataclasses.replace(policy, dialect="oracle")
    fetch = "SELECT t0.a FROM t t0 WHERE t0.tenant_code = :p FETCH FIRST 5000 ROWS ONLY"
    with pytest.raises(SqlSafetyError):
        validate_sql(fetch, oracle_policy)
