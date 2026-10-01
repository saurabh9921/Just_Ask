"""The DatabaseAdapter contract. EVERY connector must pass this suite unchanged.

Backends:
* ``sqlite``     - always runs (temporary file in a sandbox directory).
* ``postgresql`` - runs when ``TEST_POSTGRES_ADMIN_URL`` is set: a throwaway database
  is created with the demo schema and a dedicated SELECT-only role, and the adapter
  connects *as that role* (never as the admin).
* ``mysql``      - runs when ``TEST_MYSQL_ADMIN_URL`` is set
  (e.g. ``mysql+aiomysql://root:pw@127.0.0.1:3306/``): throwaway database + an account
  with ``SELECT`` on it only.
* ``sqlserver``  - runs when ``TEST_MSSQL_ADMIN_URL`` is set (e.g.
  ``mssql+aioodbc://sa:pw@127.0.0.1:1433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes``):
  throwaway database + a login that is only ``db_datareader``. The adapter's ODBC
  driver comes from ``TEST_MSSQL_ODBC_DRIVER`` (default ODBC Driver 18).

Adding a connector: add a backend fixture branch below; do not weaken assertions.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Select, TableClause, column, func, literal, select, table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import (
    QueryCancelledError,
    QueryTimeoutError,
    UnsafeStatementError,
)
from app.connectors.common.types import (
    CanonicalType,
    ConnectionConfig,
    ConnectorType,
    ObjectKind,
    TlsMode,
)
from app.connectors.factory import ConnectorFactory
from tests.conftest import ERP_DDL, build_sqlite_erp, make_settings
from tests.sync import run_sync

pytestmark = pytest.mark.contract

ORDERS = 50


@dataclass
class Backend:
    name: str
    adapter: DatabaseAdapter
    schema: str


def _config(**kwargs: Any) -> ConnectionConfig:
    base: dict[str, Any] = {
        "connection_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "host": None,
        "port": None,
        "schema": None,
        "username": None,
        "password": None,
        "max_rows": 20,
        "query_timeout_seconds": 10,
        "pool_max_size": 3,
    }
    base.update(kwargs)
    return ConnectionConfig(**base)


# ------------------------------------------------------------------ backends
PG_SEED = [
    "INSERT INTO erp.regions VALUES (1, 'North'), (2, 'South'), (3, 'West')",
    "INSERT INTO erp.customers SELECT i, 'Customer ' || i, (i % 3) + 1, 1000 * i "
    "FROM generate_series(1, 10) AS i",
    f"INSERT INTO erp.sales_orders SELECT i, (i % 10) + 1, "
    f"make_date(2026, (i % 9) + 1, 15), 100 + i, CASE WHEN i % 2 = 1 THEN 'open' ELSE 'closed' END "
    f"FROM generate_series(1, {ORDERS}) AS i",
]


async def _pg_admin(url: str, statements: list[str], *, database: str | None = None) -> None:
    target = make_url(url).set(database=database) if database else make_url(url)
    engine = create_async_engine(target, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()


@pytest.fixture(scope="module")
def pg_backend_info() -> Iterator[dict[str, str]]:
    admin = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if not admin:
        pytest.skip("TEST_POSTGRES_ADMIN_URL not set (start docker compose to run the PG contract)")
    db = f"contract_{uuid.uuid4().hex[:8]}"
    role = f"contract_ro_{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(18)
    ddl = (
        ERP_DDL.replace("CREATE TABLE ", "CREATE TABLE erp.")
        .replace("REFERENCES ", "REFERENCES erp.")
        .replace("CREATE VIEW ", "CREATE VIEW erp.")
        .replace(" FROM sales_orders", " FROM erp.sales_orders")
        .replace("JOIN customers", "JOIN erp.customers")
        .replace("JOIN regions", "JOIN erp.regions")
    )
    run_sync(_pg_admin(admin, [f'CREATE DATABASE "{db}"']))
    run_sync(
        _pg_admin(
            admin,
            [
                "CREATE SCHEMA erp",
                *[s.strip() for s in ddl.split(";") if s.strip()],
                *PG_SEED,
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE",
                f'REVOKE CREATE ON DATABASE "{db}" FROM PUBLIC',
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                f"GRANT USAGE ON SCHEMA erp TO {role}",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA erp TO {role}",
                "ANALYZE",
            ],
            database=db,
        )
    )
    url = make_url(admin)
    try:
        yield {
            "host": url.host or "127.0.0.1",
            "port": str(url.port or 5432),
            "db": db,
            "user": role,
            "password": password,
        }
    finally:
        run_sync(
            _pg_admin(
                admin,
                [f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', f"DROP ROLE IF EXISTS {role}"],
            )
        )


MYSQL_DDL = [
    "CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_name VARCHAR(50) NOT NULL)",
    "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, customer_name VARCHAR(100) NOT NULL,"
    " region_id INTEGER NOT NULL, credit_limit NUMERIC(12, 2),"
    " FOREIGN KEY (region_id) REFERENCES regions(region_id))",
    "CREATE TABLE sales_orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,"
    " order_date DATE NOT NULL, net_amount NUMERIC(12, 2) NOT NULL, status VARCHAR(20) NOT NULL,"
    " FOREIGN KEY (customer_id) REFERENCES customers(customer_id))",
    "CREATE VIEW analytics_sales_orders AS SELECT o.order_id, o.order_date, o.net_amount,"
    " o.status, r.region_name FROM sales_orders o"
    " JOIN customers c ON c.customer_id = o.customer_id"
    " JOIN regions r ON r.region_id = c.region_id",
]
MSSQL_DDL = [s.replace("VARCHAR(", "NVARCHAR(") for s in MYSQL_DDL]


async def _seed_rows(url: str, database: str | None, ddl: list[str]) -> None:
    """Create the contract schema and rows with portable bound inserts."""
    target = make_url(url).set(database=database) if database else make_url(url)
    engine = create_async_engine(target)
    async with engine.begin() as conn:
        for statement in ddl:
            await conn.execute(text(statement))
        await conn.execute(
            text("INSERT INTO regions VALUES (:i, :n)"),
            [{"i": 1, "n": "North"}, {"i": 2, "n": "South"}, {"i": 3, "n": "West"}],
        )
        await conn.execute(
            text("INSERT INTO customers VALUES (:i, :n, :r, :c)"),
            [{"i": i, "n": f"Customer {i}", "r": (i % 3) + 1, "c": 1000 * i} for i in range(1, 11)],
        )
        await conn.execute(
            text("INSERT INTO sales_orders VALUES (:i, :c, :d, :a, :s)"),
            [
                {
                    "i": i,
                    "c": (i % 10) + 1,
                    "d": date(2026, (i % 9) + 1, 15),
                    "a": 100 + i,
                    "s": "open" if i % 2 else "closed",
                }
                for i in range(1, ORDERS + 1)
            ],
        )
    await engine.dispose()


async def _admin(url: str, statements: list[str], *, database: str | None = None) -> None:
    target = make_url(url).set(database=database) if database else make_url(url)
    engine = create_async_engine(target, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()


@pytest.fixture(scope="module")
def mysql_backend_info() -> Iterator[dict[str, str]]:
    admin = os.environ.get("TEST_MYSQL_ADMIN_URL")
    if not admin:
        pytest.skip("TEST_MYSQL_ADMIN_URL not set (MySQL contract leg)")
    db = f"contract_{uuid.uuid4().hex[:8]}"
    user = f"ro_{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(18)
    run_sync(_admin(admin, [f"CREATE DATABASE `{db}`"]))
    run_sync(_seed_rows(admin, db, MYSQL_DDL))
    run_sync(
        _admin(
            admin,
            [
                f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'",
                f"GRANT SELECT ON `{db}`.* TO '{user}'@'%'",
                f"ANALYZE TABLE `{db}`.sales_orders",
            ],
        )
    )
    url = make_url(admin)
    try:
        yield {
            "host": url.host or "127.0.0.1",
            "port": str(url.port or 3306),
            "db": db,
            "user": user,
            "password": password,
        }
    finally:
        run_sync(
            _admin(admin, [f"DROP DATABASE IF EXISTS `{db}`", f"DROP USER IF EXISTS '{user}'@'%'"])
        )


@pytest.fixture(scope="module")
def mssql_backend_info() -> Iterator[dict[str, str]]:
    admin = os.environ.get("TEST_MSSQL_ADMIN_URL")
    if not admin:
        pytest.skip("TEST_MSSQL_ADMIN_URL not set (SQL Server contract leg)")
    db = f"contract_{uuid.uuid4().hex[:8]}"
    login = f"ro_{uuid.uuid4().hex[:8]}"
    password = "Aa1!" + secrets.token_urlsafe(18)
    run_sync(
        _admin(
            admin,
            [
                f"CREATE DATABASE [{db}]",
                f"CREATE LOGIN [{login}] WITH PASSWORD = '{password}', CHECK_POLICY = OFF",
            ],
        )
    )
    run_sync(_seed_rows(admin, db, MSSQL_DDL))
    run_sync(
        _admin(
            admin,
            [
                f"CREATE USER [{login}] FOR LOGIN [{login}]",
                f"ALTER ROLE db_datareader ADD MEMBER [{login}]",
            ],
            database=db,
        )
    )
    url = make_url(admin)
    try:
        yield {
            "host": url.host or "127.0.0.1",
            "port": str(url.port or 1433),
            "db": db,
            "user": login,
            "password": password,
        }
    finally:
        run_sync(
            _admin(
                admin,
                [
                    f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
                    f"DROP DATABASE [{db}]",
                    f"DROP LOGIN [{login}]",
                ],
            )
        )


@pytest.fixture(params=["sqlite", "postgresql", "mysql", "sqlserver"])
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Backend]:
    data_dir = tmp_path / "sqlite-data"
    data_dir.mkdir()
    settings: Settings = make_settings(
        f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}", data_dir
    )
    factory = ConnectorFactory(settings)
    if request.param == "sqlite":
        build_sqlite_erp(data_dir / "erp.db", orders=ORDERS)
        adapter = factory.create(_config(connector_type=ConnectorType.SQLITE, database="erp.db"))
        schema = "main"
    elif request.param in {"mysql", "sqlserver"}:
        fixture = "mysql_backend_info" if request.param == "mysql" else "mssql_backend_info"
        info = request.getfixturevalue(fixture)
        if request.param == "sqlserver":
            settings = settings.model_copy(
                update={
                    "sqlserver_odbc_driver": os.environ.get(
                        "TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"
                    )
                }
            )
            factory = ConnectorFactory(settings)
        adapter = factory.create(
            _config(
                connector_type=ConnectorType.MYSQL
                if request.param == "mysql"
                else ConnectorType.SQLSERVER,
                host=info["host"],
                port=int(info["port"]),
                database=info["db"],
                schema=None if request.param == "mysql" else "dbo",
                username=info["user"],
                password=SecretStr(info["password"]),
                tls_mode=TlsMode.DISABLE if request.param == "mysql" else TlsMode.REQUIRE,
            )
        )
        schema = info["db"] if request.param == "mysql" else "dbo"
    else:
        info = request.getfixturevalue("pg_backend_info")
        adapter = factory.create(
            _config(
                connector_type=ConnectorType.POSTGRESQL,
                host=info["host"],
                port=int(info["port"]),
                database=info["db"],
                username=info["user"],
                password=SecretStr(info["password"]),
                tls_mode=TlsMode.DISABLE,
            )
        )
        schema = "erp"
    try:
        yield Backend(request.param, adapter, schema)
    finally:
        await adapter.close()


def _schema(schema: str) -> str | None:
    # SQLite's "main" and MySQL/SQL Server default schemas need no qualification.
    return None if schema == "main" or not schema.startswith("erp") else schema


def orders(schema: str) -> TableClause:
    return table(
        "sales_orders",
        column("order_id"),
        column("customer_id"),
        column("order_date"),
        column("net_amount"),
        column("status"),
        schema=_schema(schema),
    )


def view(schema: str) -> TableClause:
    return table(
        "analytics_sales_orders",
        column("order_id"),
        column("net_amount"),
        column("region_name"),
        schema=_schema(schema),
    )


# ----------------------------------------------------------------- contract
async def test_connection_succeeds_with_read_only_posture(backend: Backend) -> None:
    result = await backend.adapter.test_connection()
    assert result.ok, result
    assert result.server_version
    assert result.error_code is None


async def test_schemas_exclude_system_schemas(backend: Backend) -> None:
    schemas = await backend.adapter.get_schemas()
    assert backend.schema in schemas
    assert not {"pg_catalog", "information_schema", "pg_toast", "temp"} & set(schemas)


async def test_tables_and_views(backend: Backend) -> None:
    tables = await backend.adapter.get_tables(backend.schema)
    views = await backend.adapter.get_views(backend.schema)
    assert {"regions", "customers", "sales_orders"} <= {t.name for t in tables}
    assert all(t.kind is ObjectKind.TABLE for t in tables)
    assert not any(t.name.startswith("sqlite_") for t in tables)
    assert "analytics_sales_orders" in {v.name for v in views}
    assert all(v.kind is ObjectKind.VIEW for v in views)


async def test_columns_are_normalised(backend: Backend) -> None:
    cols = {c.name: c for c in await backend.adapter.get_columns(backend.schema, "sales_orders")}
    assert list(cols) == ["order_id", "customer_id", "order_date", "net_amount", "status"]
    assert cols["order_id"].canonical_type is CanonicalType.INTEGER
    assert cols["order_date"].canonical_type is CanonicalType.DATE
    assert cols["net_amount"].canonical_type is CanonicalType.DECIMAL
    assert cols["status"].canonical_type is CanonicalType.STRING
    assert cols["net_amount"].nullable is False
    assert [c.ordinal for c in cols.values()] == [1, 2, 3, 4, 5]


async def test_keys(backend: Backend) -> None:
    assert await backend.adapter.get_primary_keys(backend.schema, "sales_orders") == ["order_id"]
    (fk,) = await backend.adapter.get_foreign_keys(backend.schema, "sales_orders")
    assert fk.columns == ["customer_id"]
    assert fk.referred_table == "customers"
    assert fk.referred_columns == ["customer_id"]


async def test_catalog_snapshot_is_structure_only(backend: Backend) -> None:
    catalog = await backend.adapter.get_catalog([backend.schema])
    by_name = {o.name: o for o in catalog.objects}
    assert {"regions", "customers", "sales_orders", "analytics_sales_orders"} <= set(by_name)
    assert by_name["analytics_sales_orders"].kind is ObjectKind.VIEW
    assert by_name["sales_orders"].primary_key == ["order_id"]
    assert by_name["sales_orders"].foreign_keys[0].referred_table == "customers"
    assert catalog.server_version
    dumped = catalog.model_dump_json()
    assert "Customer 1" not in dumped and "North" not in dumped  # no data values


async def test_execute_readonly_caps_rows_and_flags_truncation(backend: Backend) -> None:
    o = orders(backend.schema)
    query: Select[Any] = select(o.c.order_id, o.c.net_amount).order_by(o.c.order_id)
    result = await backend.adapter.execute_readonly(query, max_rows=10)
    assert result.row_count == 10
    assert result.truncated is True
    assert [c.name for c in result.columns] == ["order_id", "net_amount"]
    assert result.rows[0][0] == 1


async def test_connection_max_rows_is_a_hard_ceiling(backend: Backend) -> None:
    o = orders(backend.schema)
    result = await backend.adapter.execute_readonly(select(o.c.order_id), max_rows=10_000)
    assert result.row_count == backend.adapter.config.max_rows
    assert result.truncated is True


async def test_aggregation_through_reporting_view(backend: Backend) -> None:
    v = view(backend.schema)
    query = (
        select(v.c.region_name, func.sum(v.c.net_amount).label("total_sales"))
        .group_by(v.c.region_name)
        .order_by(v.c.region_name)
    )
    result = await backend.adapter.execute_readonly(query)
    assert result.truncated is False
    assert [r[0] for r in result.rows] == ["North", "South", "West"]
    assert float(sum(float(r[1]) for r in result.rows)) == sum(
        100.0 + i for i in range(1, ORDERS + 1)
    )


async def test_bound_parameters_are_not_interpolated(backend: Backend) -> None:
    o = orders(backend.schema)
    hostile = "open' OR '1'='1"
    query = select(func.count()).select_from(o).where(o.c.status == hostile)
    result = await backend.adapter.execute_readonly(query)
    assert result.rows[0][0] == 0


async def test_only_select_objects_are_executable(backend: Backend) -> None:
    for bad in (text("SELECT 1"), "SELECT 1", text("DELETE FROM sales_orders")):
        with pytest.raises(UnsafeStatementError):
            await backend.adapter.execute_readonly(bad)  # type: ignore[arg-type]


async def test_locking_reads_rejected(backend: Backend) -> None:
    o = orders(backend.schema)
    with pytest.raises(UnsafeStatementError):
        await backend.adapter.execute_readonly(select(o.c.order_id).with_for_update())


async def test_session_is_read_only_even_for_raw_writes(backend: Backend) -> None:
    """Defence in depth: bypass our API entirely and try to write on the adapter's pool."""
    engine = backend.adapter.engine  # type: ignore[attr-defined]
    target = f"{backend.schema}.sales_orders" if _schema(backend.schema) else "sales_orders"
    with pytest.raises(Exception) as info:
        async with engine.connect() as conn:
            await conn.execute(text(f"DELETE FROM {target}"))
            await conn.commit()
    message = str(info.value).lower()
    # SQL Server has no read-only session: the login's missing DELETE permission stops it.
    assert any(
        w in message
        for w in (
            "read-only",
            "readonly",
            "read only",
            "permission denied",
            "permission was denied",
        )
    )
    o = orders(backend.schema)
    count = await backend.adapter.execute_readonly(select(func.count()).select_from(o))
    assert count.rows[0][0] == ORDERS


async def test_stream_readonly_is_bounded(backend: Backend) -> None:
    o = orders(backend.schema)
    batches = [
        batch
        async for batch in backend.adapter.stream_readonly(
            select(o.c.order_id).order_by(o.c.order_id),
            batch_size=7,
            max_rows=15,
        )
    ]
    assert all(len(b) <= 7 for b in batches)
    assert sum(len(b) for b in batches) == 15


def _slow_query(backend: Backend) -> Select[Any]:
    if backend.name == "mysql":  # recursive CTEs are capped at 1000 iterations
        return select(func.sleep(30).label("slept"))
    if backend.name == "sqlserver":  # MAXRECURSION defaults to 100; cross-join catalog views
        a, b, c = (table("all_objects", column("object_id"), schema="sys").alias(n) for n in "abc")
        return select(func.count()).select_from(a.join(b, literal(1) == 1).join(c, literal(1) == 1))
    seq = select(literal(1).label("n")).cte("seq", recursive=True)
    seq = seq.union_all(select(seq.c.n + 1).where(seq.c.n < 1_000_000_000))
    return select(func.count()).select_from(seq)


async def test_timeout_is_enforced(backend: Backend) -> None:
    with pytest.raises(QueryTimeoutError):
        await backend.adapter.execute_readonly(_slow_query(backend), timeout_seconds=0.5)


async def test_cancellation(backend: Backend) -> None:
    task = asyncio.create_task(
        backend.adapter.execute_readonly(
            _slow_query(backend), timeout_seconds=10, query_id="q-cancel"
        )
    )
    for _ in range(50):
        await asyncio.sleep(0.05)
        if await backend.adapter.cancel_query("q-cancel"):
            break
    with pytest.raises(QueryCancelledError):
        await task
    assert await backend.adapter.cancel_query("q-unknown") is False


async def test_explain_returns_a_plan(backend: Backend) -> None:
    o = orders(backend.schema)
    plan = await backend.adapter.explain_query(select(o.c.order_id).where(o.c.status == "open"))
    assert plan.plan


QUOTED = {"mysql": '`we"ird`', "sqlserver": '[we"ird]'}


def test_quote_identifier_escapes(backend: Backend) -> None:
    expected = QUOTED.get(backend.name, '"we""ird"')
    assert backend.adapter.quote_identifier('we"ird') == expected
    with pytest.raises(UnsafeStatementError):
        backend.adapter.quote_identifier("bad\x00name")
