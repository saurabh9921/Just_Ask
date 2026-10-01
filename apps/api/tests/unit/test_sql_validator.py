"""SQL safety validator: accepts compiler output for every dialect, rejects each
violation with a stable reason code."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from sqlalchemy import (
    Date,
    Float,
    Numeric,
    Select,
    String,
    bindparam,
    cast,
    column,
    distinct,
    func,
    literal_column,
    select,
    table,
)
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.dialects.sqlite import aiosqlite
from sqlalchemy.engine import Dialect

from app.query.sql_validator import SqlPolicy, SqlSafetyError, validate_sql

ORDERS = table(
    "analytics_sales_orders",
    column("region_name", String),
    column("net_amount", Numeric),
    column("order_date", Date),
    column("tenant_code", String),
    column("customer_id", String),
    schema="analytics",
)
CUSTOMERS = table(
    "analytics_customers",
    column("customer_id", String),
    column("segment", String),
    schema="analytics",
)

DIALECTS: dict[str, Dialect] = {
    "postgresql": asyncpg.dialect(),  # type: ignore[no-untyped-call]
    "sqlite": aiosqlite.dialect(),
    "mysql": mysql.dialect(),
    "sqlserver": mssql.dialect(),  # type: ignore[no-untyped-call]
}


def policy(dialect: str, **overrides: Any) -> SqlPolicy:
    values: dict[str, Any] = {
        "dialect": dialect,
        "allowed_tables": frozenset({"analytics.analytics_sales_orders", "analytics_customers"}),
        "allowed_columns": frozenset(
            {"region_name", "net_amount", "order_date", "tenant_code", "customer_id", "segment"}
        ),
        "required_equality_columns": frozenset({"tenant_code"}),
    }
    values.update(overrides)
    return SqlPolicy(**values)


def bucket(dialect: str, grain: str = "month") -> Any:
    if dialect == "sqlite":
        if grain == "week":
            return func.date(
                ORDERS.c.order_date, literal_column("'weekday 0'"), literal_column("'-6 days'")
            )
        return func.strftime(literal_column("'%Y-%m-01'"), ORDERS.c.order_date)
    return func.date_trunc(literal_column(f"'{grain}'"), ORDERS.c.order_date)


def compiled_statement(dialect: str, *, grain: str = "month", join: bool = True) -> Select[Any]:
    total = func.sum(ORDERS.c.net_amount).label("total_sales")
    ratio = (
        cast(func.sum(ORDERS.c.net_amount), Float)
        / func.nullif(func.count(ORDERS.c.customer_id), 0)
    ).label("aov")
    pct = (
        literal_column("100.0")
        * func.sum(ORDERS.c.net_amount)
        / func.nullif(func.sum(ORDERS.c.net_amount), 0)
    ).label("pct")
    period = bucket(dialect, grain)
    cols: list[Any] = [
        ORDERS.c.region_name,
        period.label("period"),
        total,
        func.count(distinct(ORDERS.c.customer_id)).label("customers"),
        ratio,
        pct,
        func.avg(ORDERS.c.net_amount).label("avg_amount"),
        func.min(ORDERS.c.net_amount).label("min_amount"),
        func.max(ORDERS.c.net_amount).label("max_amount"),
        func.coalesce(func.sum(ORDERS.c.net_amount), 0).label("safe_total"),
    ]
    group: list[Any] = [ORDERS.c.region_name, period]
    source: Any = ORDERS
    conditions: list[Any] = [
        ORDERS.c.tenant_code == bindparam("p_tenant", "demo"),
        ORDERS.c.order_date >= bindparam("p_start", datetime.date(2026, 1, 1)),
        ORDERS.c.order_date < bindparam("p_end", datetime.date(2026, 7, 1)),
        ORDERS.c.region_name.in_(bindparam("p_regions", ["West", "East"], expanding=True)),
    ]
    if join:
        cols.insert(1, CUSTOMERS.c.segment)
        group.insert(1, CUSTOMERS.c.segment)
        source = ORDERS.join(CUSTOMERS, ORDERS.c.customer_id == CUSTOMERS.c.customer_id)
        conditions.append(func.lower(CUSTOMERS.c.segment).like(bindparam("p_like", "%retail%")))
    return (
        select(*cols)
        .select_from(source)
        .where(*conditions)
        .group_by(*group)
        .order_by(literal_column("total_sales").desc())
        .limit(100)
    )


def render(dialect: str, stmt: Select[Any]) -> str:
    return str(stmt.compile(dialect=DIALECTS[dialect], compile_kwargs={"render_postcompile": True}))


def reason(sql: str, pol: SqlPolicy) -> str:
    with pytest.raises(SqlSafetyError) as info:
        validate_sql(sql, pol)
    assert info.value.code == "QUERY_NOT_ALLOWED"
    assert info.value.status_code == 403
    return str(info.value.details["reason"])


# ------------------------------------------------------------------ accepts
@pytest.mark.filterwarnings("ignore:Datatype FLOAT does not support CAST on MySQL")
@pytest.mark.parametrize("dialect", list(DIALECTS))
@pytest.mark.parametrize("grain", ["month", "week"])
@pytest.mark.parametrize("join", [True, False])
def test_compiler_output_is_accepted(dialect: str, grain: str, join: bool) -> None:
    sql = render(dialect, compiled_statement(dialect, grain=grain, join=join))
    result = validate_sql(sql, policy(dialect))
    assert "analytics.analytics_sales_orders" in result.tables
    assert ("analytics.analytics_customers" in result.tables) is join
    assert {"region_name", "net_amount", "tenant_code"} <= result.columns
    assert result.has_limit
    assert len(result.statement_hash) == 64


def test_reported_functions_are_the_allowlisted_ones() -> None:
    sql = render("postgresql", compiled_statement("postgresql"))
    functions = validate_sql(sql, policy("postgresql")).functions
    assert {"sum", "count", "avg", "min", "max", "nullif", "cast", "coalesce", "lower"} <= functions
    assert "timestamptrunc" in functions or "datetrunc" in functions


def test_statement_hash_is_stable_and_whitespace_insensitive() -> None:
    sql = render("sqlite", compiled_statement("sqlite"))
    a = validate_sql(sql, policy("sqlite")).statement_hash
    b = validate_sql(
        "  " + sql.replace(" ", "   ").replace("\n", " \n "), policy("sqlite")
    ).statement_hash
    c = validate_sql(sql.replace("100", "50"), policy("sqlite")).statement_hash
    assert a == b
    assert a != c


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region_name FROM analytics_customers JOIN analytics.analytics_sales_orders "
        "USING (customer_id) WHERE tenant_code = ? LIMIT 10",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = :tc AND region_name = 'O''Brien' LIMIT ?",
        "SELECT region_name FROM analytics_sales_orders WHERE (tenant_code = $1) AND net_amount > 5 LIMIT 5",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = %(tenant)s LIMIT 5",
        "SELECT region_name FROM analytics_sales_orders WHERE region_name = 'café; -- /* not a comment */' AND tenant_code = ? LIMIT 5",
        "SELECT datetime(order_date, 'start of month') AS m FROM analytics_sales_orders WHERE tenant_code = ? LIMIT 5",
    ],
)
def test_other_valid_shapes(sql: str) -> None:
    dialect = "postgresql" if "$1" in sql else "sqlite"
    validate_sql(sql, policy(dialect))


def test_mysql_percent_placeholders_and_percent_literals() -> None:
    sql = (
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = %s "
        "AND region_name LIKE '%West%' LIMIT %s"
    )
    validate_sql(sql, policy("mysql"))


def test_select_alias_allowed_in_order_by_only() -> None:
    ok = "SELECT sum(net_amount) AS total FROM analytics_sales_orders WHERE tenant_code = ? ORDER BY total LIMIT 5"
    validate_sql(ok, policy("sqlite"))
    bad = "SELECT sum(net_amount) AS total FROM analytics_sales_orders WHERE tenant_code = ? AND total > 1 LIMIT 5"
    assert reason(bad, policy("sqlite")) == "COLUMN_NOT_ALLOWED"


def test_no_required_filter_policy() -> None:
    sql = "SELECT region_name FROM analytics_sales_orders LIMIT 5"
    validate_sql(sql, policy("sqlite", required_equality_columns=frozenset()))


# ---------------------------------------------------------------- rejections
BASE_WHERE = "WHERE tenant_code = ?"

REJECTIONS: list[tuple[str, str, str]] = [
    (
        "MULTIPLE_STATEMENTS",
        "sqlite",
        f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} LIMIT 5; SELECT 1",
    ),
    (
        "COMMENT",
        "sqlite",
        f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} LIMIT 5 -- x",
    ),
    (
        "COMMENT",
        "sqlite",
        f"SELECT region_name /* x */ FROM analytics_sales_orders {BASE_WHERE} LIMIT 5",
    ),
    (
        "COMMENT",
        "mysql",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = ? LIMIT 5 # x",
    ),
    ("PARSE_ERROR", "sqlite", "SELECT region_name FROM WHERE LIMIT"),
    (
        "PARSE_ERROR",
        "sqlite",
        "SELECT region_name FROM analytics_sales_orders WHERE region_name = 'unterminated",
    ),
    (
        "PARSE_ERROR",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5；",  # noqa: RUF001 - intentional unicode payload
    ),
    (
        "NOT_SELECT",
        "sqlite",
        f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} UNION SELECT segment FROM analytics_customers LIMIT 5",
    ),
    (
        "NOT_SELECT",
        "postgresql",
        "WITH x AS (SELECT region_name FROM analytics_sales_orders) SELECT region_name FROM x LIMIT 5",
    ),
    (
        "NOT_SELECT",
        "postgresql",
        "SELECT region_name INTO new_table FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5",
    ),
    ("WRITE_OR_DDL", "sqlite", "DELETE FROM analytics_sales_orders WHERE tenant_code = ?"),
    ("WRITE_OR_DDL", "postgresql", "UPDATE analytics_sales_orders SET net_amount = 0"),
    ("WRITE_OR_DDL", "postgresql", "DROP TABLE analytics_sales_orders"),
    ("WRITE_OR_DDL", "postgresql", "GRANT SELECT ON analytics_sales_orders TO public"),
    ("WRITE_OR_DDL", "postgresql", "CALL refresh_all()"),
    ("WRITE_OR_DDL", "sqlserver", "EXEC xp_cmdshell 'dir'"),
    (
        "WRITE_OR_DDL",
        "postgresql",
        "WITH x AS (DELETE FROM analytics_sales_orders RETURNING region_name) SELECT region_name FROM x LIMIT 5",
    ),
    (
        "SUBQUERY",
        "sqlite",
        f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} AND customer_id IN (SELECT customer_id FROM analytics_customers) LIMIT 5",
    ),
    ("STAR", "sqlite", f"SELECT * FROM analytics_sales_orders {BASE_WHERE} LIMIT 5"),
    ("STAR", "sqlite", f"SELECT count(*) FROM analytics_sales_orders {BASE_WHERE} LIMIT 5"),
    (
        "STAR",
        "postgresql",
        "SELECT analytics_sales_orders.* FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5",
    ),
    ("TABLE_NOT_ALLOWED", "sqlite", f"SELECT region_name FROM payroll {BASE_WHERE} LIMIT 5"),
    (
        "TABLE_NOT_ALLOWED",
        "postgresql",
        "SELECT region_name FROM other.analytics_sales_orders WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "SYSTEM_CATALOG",
        "postgresql",
        "SELECT usename FROM pg_catalog.pg_user WHERE tenant_code = $1 LIMIT 5",
    ),
    ("SYSTEM_CATALOG", "sqlite", f"SELECT sql FROM sqlite_master {BASE_WHERE} LIMIT 5"),
    (
        "SYSTEM_CATALOG",
        "mysql",
        "SELECT table_name FROM information_schema.tables WHERE tenant_code = ? LIMIT 5",
    ),
    (
        "COLUMN_NOT_ALLOWED",
        "sqlite",
        f"SELECT customer_bank_account FROM analytics_sales_orders {BASE_WHERE} LIMIT 5",
    ),
    (
        "FUNCTION_NOT_ALLOWED",
        "postgresql",
        "SELECT pg_sleep(10) AS s FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "FUNCTION_NOT_ALLOWED",
        "sqlite",
        f"SELECT load_extension('x') AS e FROM analytics_sales_orders {BASE_WHERE} LIMIT 5",
    ),
    (
        "FUNCTION_NOT_ALLOWED",
        "postgresql",
        "SELECT version() AS v FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "CROSS_JOIN",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders CROSS JOIN analytics_customers WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "CROSS_JOIN",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders, analytics_customers WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "CROSS_JOIN",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders NATURAL JOIN analytics_customers WHERE tenant_code = $1 LIMIT 5",
    ),
    (
        "CROSS_JOIN",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders JOIN analytics_customers WHERE tenant_code = $1 LIMIT 5",
    ),
    ("MISSING_LIMIT", "sqlite", f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE}"),
    (
        "LIMIT_TOO_HIGH",
        "sqlite",
        f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} LIMIT 5000",
    ),
    (
        "LIMIT_TOO_HIGH",
        "sqlserver",
        "SELECT TOP 5000 region_name FROM analytics_sales_orders WHERE tenant_code = :tc",
    ),
    ("MISSING_REQUIRED_FILTER", "sqlite", "SELECT region_name FROM analytics_sales_orders LIMIT 5"),
    (
        "MISSING_REQUIRED_FILTER",
        "sqlite",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = 'demo' LIMIT 5",
    ),
    (
        "MISSING_REQUIRED_FILTER",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = $1 OR 1 = 1 LIMIT 5",
    ),
    (
        "LOCKING",
        "postgresql",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = $1 LIMIT 5 FOR UPDATE",
    ),
]


@pytest.mark.parametrize(
    ("expected", "dialect", "sql"),
    REJECTIONS,
    ids=[f"{r[0]}-{i}" for i, r in enumerate(REJECTIONS)],
)
def test_rejections(expected: str, dialect: str, sql: str) -> None:
    assert reason(sql, policy(dialect)) == expected


def test_too_many_joins() -> None:
    sql = render("postgresql", compiled_statement("postgresql"))
    assert reason(sql, policy("postgresql", max_joins=0)) == "TOO_MANY_JOINS"


def test_limit_ceiling_is_policy_driven() -> None:
    sql = f"SELECT region_name FROM analytics_sales_orders {BASE_WHERE} LIMIT 50"
    assert reason(sql, policy("sqlite", max_limit=10)) == "LIMIT_TOO_HIGH"


def test_unknown_dialect_rejected() -> None:
    assert reason("SELECT 1", policy("snowflake")) == "PARSE_ERROR"


def test_every_documented_reason_is_covered() -> None:
    covered = {r[0] for r in REJECTIONS} | {"TOO_MANY_JOINS"}
    documented = {
        "MULTIPLE_STATEMENTS",
        "COMMENT",
        "PARSE_ERROR",
        "NOT_SELECT",
        "WRITE_OR_DDL",
        "SUBQUERY",
        "STAR",
        "TABLE_NOT_ALLOWED",
        "SYSTEM_CATALOG",
        "COLUMN_NOT_ALLOWED",
        "FUNCTION_NOT_ALLOWED",
        "CROSS_JOIN",
        "TOO_MANY_JOINS",
        "MISSING_LIMIT",
        "LIMIT_TOO_HIGH",
        "MISSING_REQUIRED_FILTER",
        "LOCKING",
    }
    assert documented <= covered
