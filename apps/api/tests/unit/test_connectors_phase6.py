"""Phase 6 connectors and performance controls (no database servers needed).

Live behaviour of MySQL / SQL Server is covered by the contract suite when
``TEST_MYSQL_ADMIN_URL`` / ``TEST_MSSQL_ADMIN_URL`` are set; these tests pin down the
parts that must hold regardless: engine/driver configuration, read-only posture
logic, error classification, circuit breaker, per-connection concurrency limit,
read-replica routing and the export ceiling.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ssl
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import column, func, select, table
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.connectors.common.errors import (
    ConnectionConfigError,
    ConnectorBusyError,
    ConnectorCircuitOpenError,
    ConnectorError,
    ConnectorErrorCode,
    QueryTimeoutError,
    UnsupportedConnectorError,
)
from app.connectors.common.sqlalchemy_adapter import PostureError, classify_error
from app.connectors.common.types import ConnectionConfig, ConnectorType, TlsMode
from app.connectors.factory import ConnectorFactory
from app.connectors.mysql.adapter import MySQLAdapter, assess_grants, build_mysql_ssl
from app.connectors.oracle.adapter import OracleAdapter
from app.connectors.postgresql.adapter import PostgreSQLAdapter
from app.connectors.registry import get_spec, supported_types
from app.connectors.sap_hana.adapter import SapHanaAdapter
from app.connectors.sqlite.adapter import SQLiteAdapter
from app.connectors.sqlserver.adapter import SQLServerAdapter, build_odbc_connect
from app.core.permissions import Role
from app.core.resilience import CircuitState
from tests.conftest import bearer, build_sqlite_erp, create_account, login, make_settings


def config(**kwargs: Any) -> ConnectionConfig:
    base: dict[str, Any] = {
        "connection_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "connector_type": ConnectorType.POSTGRESQL,
        "host": "db.internal",
        "port": None,
        "database": "erp",
        "schema": None,
        "username": "reader",
        "password": SecretStr("p@ss;word}"),
    }
    base.update(kwargs)
    return ConnectionConfig(**base)


# ------------------------------------------------------------------ registry
def test_registry_maturity_and_gating() -> None:
    assert supported_types() == [
        ConnectorType.MYSQL,
        ConnectorType.POSTGRESQL,
        ConnectorType.SQLITE,
        ConnectorType.SQLSERVER,
    ]
    assert ConnectorType.ORACLE in supported_types(allow_foundation=True)
    assert get_spec(ConnectorType.ORACLE, allow_foundation=False) is None
    assert get_spec(ConnectorType.SAP_HANA) is not None
    assert get_spec(ConnectorType.SNOWFLAKE) is None
    assert get_spec(ConnectorType.BIGQUERY) is None


def test_factory_builds_each_adapter(tmp_path: Path) -> None:
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", tmp_path)
    factory = ConnectorFactory(settings)
    assert isinstance(factory.create(config(connector_type=ConnectorType.MYSQL)), MySQLAdapter)
    mssql = factory.create(config(connector_type=ConnectorType.SQLSERVER))
    assert isinstance(mssql, SQLServerAdapter)
    with pytest.raises(UnsupportedConnectorError):
        factory.create(config(connector_type=ConnectorType.ORACLE))  # foundation: disabled
    enabled = ConnectorFactory(settings.model_copy(update={"enable_foundation_connectors": True}))
    assert isinstance(enabled.create(config(connector_type=ConnectorType.ORACLE)), OracleAdapter)
    assert isinstance(enabled.create(config(connector_type=ConnectorType.SAP_HANA)), SapHanaAdapter)
    with pytest.raises(UnsupportedConnectorError):
        enabled.create(config(connector_type=ConnectorType.SNOWFLAKE))


async def test_foundation_connectors_are_rejected_by_the_api(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
            ),
        )
    )
    for connector in ("oracle", "sap_hana", "snowflake"):
        r = await client.post(
            "/api/v1/connections",
            json={
                "name": f"x-{connector}",
                "connector_type": connector,
                "host": "db.example.com",
                "database_name": "erp",
                "username": "reader",
            },
            headers=admin,
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "CONNECTOR_NOT_SUPPORTED"


# ------------------------------------------------------------------ MySQL
def test_mysql_engine_is_read_only_by_default() -> None:
    adapter = MySQLAdapter(config(connector_type=ConnectorType.MYSQL, tls_mode=TlsMode.DISABLE))
    url = adapter.engine.url
    assert url.drivername == "mysql+aiomysql"
    assert url.port == 3306 and url.query["charset"] == "utf8mb4"
    assert adapter.quote_identifier('we"ird') == '`we"ird`'


def test_mysql_tls_contexts() -> None:
    assert build_mysql_ssl(TlsMode.DISABLE, None) is None
    required = build_mysql_ssl(TlsMode.REQUIRE, None)
    assert required is not None and required.verify_mode is ssl.CERT_NONE
    verify_ca = build_mysql_ssl(TlsMode.VERIFY_CA, None)
    assert verify_ca is not None and not verify_ca.check_hostname
    full = build_mysql_ssl(TlsMode.VERIFY_FULL, None)
    assert full is not None and full.check_hostname and full.verify_mode is ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "grants",
    [
        ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%`"],
        ["GRANT SELECT, INSERT ON *.* TO `app`@`%`"],
        ["GRANT SUPER ON *.* TO `ops`@`%`"],
    ],
)
def test_mysql_global_write_grants_are_refused(grants: list[str]) -> None:
    with pytest.raises(PostureError) as info:
        assess_grants(grants, "erp")
    assert info.value.code is ConnectorErrorCode.ROLE_TOO_PRIVILEGED


def test_mysql_database_grants() -> None:
    assert (
        assess_grants(
            ["GRANT USAGE ON *.* TO `r`@`%`", "GRANT SELECT ON `erp`.* TO `r`@`%`"], "erp"
        )
        == []
    )
    warnings = assess_grants(["GRANT SELECT, UPDATE ON `erp`.* TO `r`@`%`"], "erp")
    assert warnings and "modify" in warnings[0]


# ------------------------------------------------------------------ SQL Server
def test_sqlserver_connection_string_is_read_only_and_escaped() -> None:
    cfg = config(connector_type=ConnectorType.SQLSERVER, port=1444, tls_mode=TlsMode.VERIFY_FULL)
    conn = build_odbc_connect(cfg, "ODBC Driver 18 for SQL Server")
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in conn
    assert "SERVER=db.internal,1444" in conn
    assert "ApplicationIntent=ReadOnly" in conn
    assert "Encrypt=yes;TrustServerCertificate=no" in conn
    assert "PWD={p@ss;word}}}" in conn  # ';' is inert inside braces, '}' is doubled
    required = build_odbc_connect(dataclasses.replace(cfg, tls_mode=TlsMode.REQUIRE), "D")
    assert "TrustServerCertificate=yes" in required


@pytest.mark.parametrize("host", ["db;Trusted_Connection=yes", "db}", "db=1"])
def test_sqlserver_host_injection_is_rejected(host: str) -> None:
    with pytest.raises(ConnectionConfigError):
        build_odbc_connect(config(connector_type=ConnectorType.SQLSERVER, host=host), "D")


def test_sqlserver_adapter_uses_configured_driver(tmp_path: Path) -> None:
    settings = make_settings(
        f"sqlite+aiosqlite:///{tmp_path / 'a.db'}",
        tmp_path,
        sqlserver_odbc_driver="ODBC Driver 17 for SQL Server",
    )
    adapter = ConnectorFactory(settings).create(config(connector_type=ConnectorType.SQLSERVER))
    assert isinstance(adapter, SQLServerAdapter)
    odbc = adapter.engine.url.query["odbc_connect"]
    assert isinstance(odbc, str) and "ODBC Driver 17 for SQL Server" in odbc
    assert adapter.quote_identifier("a]b") == "[a]]b]"


async def test_sqlserver_explain_is_not_supported() -> None:
    adapter = SQLServerAdapter(config(connector_type=ConnectorType.SQLSERVER))
    with pytest.raises(UnsupportedConnectorError):
        await adapter.explain_query(select(column("a")).select_from(table("t")))


# ------------------------------------------------------------------ Oracle / HANA
def test_oracle_engine_uses_service_name_and_tcps() -> None:
    adapter = OracleAdapter(
        config(connector_type=ConnectorType.ORACLE, database="ERPPDB", tls_mode=TlsMode.VERIFY_FULL)
    )
    url = adapter.engine.url
    assert url.drivername == "oracle+oracledb" and url.port == 1521
    assert url.query["service_name"] == "ERPPDB"


async def test_hana_foundation_compiles_but_does_not_execute() -> None:
    adapter = SapHanaAdapter(config(connector_type=ConnectorType.SAP_HANA))
    assert adapter.dialect.name == "hana"
    assert adapter.quote_identifier('a"b') == '"a""b"'
    assert adapter.normalize_type("SECONDDATE").value == "timestamp"
    result = await adapter.test_connection()
    assert not result.ok and result.error_code == "CONNECTOR_NOT_AVAILABLE"
    with pytest.raises(UnsupportedConnectorError):
        await adapter.execute_readonly(select(column("a")))
    with pytest.raises(UnsupportedConnectorError):
        await adapter.get_catalog()


# ------------------------------------------------------------------ error classification
class _DriverError(Exception):
    pass


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ((1045, "Access denied"), ConnectorErrorCode.AUTH_FAILED),
        ((2003, "Can't connect"), ConnectorErrorCode.HOST_UNREACHABLE),
        ((3024, "max_execution_time exceeded"), ConnectorErrorCode.TIMEOUT),
        ((1792, "READ ONLY transaction"), ConnectorErrorCode.NOT_READ_ONLY),
        (("28000", "Login failed"), ConnectorErrorCode.AUTH_FAILED),
        (("HYT00", "Query timeout expired"), ConnectorErrorCode.TIMEOUT),
        (("08001", "TCP Provider"), ConnectorErrorCode.HOST_UNREACHABLE),
        (("ORA-01017: invalid username/password",), ConnectorErrorCode.AUTH_FAILED),
        (("ORA-12541: no listener",), ConnectorErrorCode.HOST_UNREACHABLE),
    ],
)
def test_driver_errors_are_classified(args: tuple[Any, ...], code: ConnectorErrorCode) -> None:
    wrapped = OperationalError("stmt", {}, _DriverError(*args))
    assert classify_error(wrapped) is code


# ------------------------------------------------------------------ performance controls
def _sqlite_adapter(tmp_path: Path, **overrides: Any) -> SQLiteAdapter:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    build_sqlite_erp(data / "erp.db", orders=60)
    settings: Settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", data)
    adapter = ConnectorFactory(settings).create(
        config(
            connector_type=ConnectorType.SQLITE,
            host=None,
            database="erp.db",
            username=None,
            password=None,
            max_rows=5,
            **overrides,
        )
    )
    assert isinstance(adapter, SQLiteAdapter)
    return adapter


ORDERS = table("sales_orders", column("order_id"), column("net_amount"))


async def test_stream_ceiling_overrides_connection_max_rows(tmp_path: Path) -> None:
    adapter = _sqlite_adapter(tmp_path)
    try:
        rows = [
            r
            async for b in adapter.stream_readonly(select(ORDERS.c.order_id), batch_size=7)
            for r in b
        ]
        assert len(rows) == 5  # connection max_rows
        exported = [
            r
            async for b in adapter.stream_readonly(
                select(ORDERS.c.order_id), batch_size=7, ceiling=40
            )
            for r in b
        ]
        assert len(exported) == 40
    finally:
        await adapter.close()


async def test_concurrency_limit_rejects_when_the_pool_is_saturated(tmp_path: Path) -> None:
    adapter = _sqlite_adapter(tmp_path, pool_max_size=1, queue_timeout_seconds=0.1)
    try:
        await adapter._semaphore.acquire()  # another statement holds the only slot
        with pytest.raises(ConnectorBusyError):
            await adapter.execute_readonly(select(func.count()).select_from(ORDERS))
        adapter._semaphore.release()
        result = await adapter.execute_readonly(select(func.count()).select_from(ORDERS))
        assert result.rows[0][0] == 60
    finally:
        await adapter.close()


async def test_circuit_breaker_opens_after_repeated_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _sqlite_adapter(tmp_path, breaker_failure_threshold=2, breaker_reset_seconds=60)
    clock = [1000.0]
    adapter.breaker.clock = lambda: clock[0]

    async def hang(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(5)

    try:
        original = adapter._begin_readonly
        monkeypatch.setattr(adapter, "_begin_readonly", hang)
        query = select(func.count()).select_from(ORDERS)
        for _ in range(2):
            with pytest.raises(QueryTimeoutError):
                await adapter.execute_readonly(query, timeout_seconds=0.1)
        with pytest.raises(ConnectorCircuitOpenError) as info:
            await adapter.execute_readonly(query)
        assert info.value.status_code == 503
        monkeypatch.setattr(adapter, "_begin_readonly", original)
        clock[0] += 61  # half-open: one trial call, success closes the breaker
        assert (await adapter.execute_readonly(query)).rows[0][0] == 60
        assert (await adapter.execute_readonly(query)).rows[0][0] == 60
    finally:
        await adapter.close()


async def test_breaker_ignores_errors_where_the_database_answered(tmp_path: Path) -> None:
    adapter = _sqlite_adapter(tmp_path, breaker_failure_threshold=1)
    try:
        missing = table("no_such_table", column("x"))
        for _ in range(3):
            with pytest.raises(Exception):  # noqa: B017 - any connector error
                await adapter.execute_readonly(select(missing.c.x))
        assert (await adapter.execute_readonly(select(func.count()).select_from(ORDERS))).rows
    finally:
        await adapter.close()


def test_read_replica_routes_queries_but_not_metadata() -> None:
    adapter = PostgreSQLAdapter(
        config(
            read_replica_host="replica.internal", read_replica_port=6543, tls_mode=TlsMode.DISABLE
        )
    )
    assert adapter.engine.url.host == "db.internal"
    assert adapter.query_engine.url.host == "replica.internal"
    assert adapter.query_engine.url.port == 6543
    plain = PostgreSQLAdapter(config(tls_mode=TlsMode.DISABLE))
    assert plain.query_engine is plain.engine


async def test_replica_fields_round_trip_through_the_api(
    app: FastAPI, client: httpx.AsyncClient, sqlite_data_dir: Path
) -> None:
    admin = bearer(
        await login(
            client,
            await create_account(
                app, tenant_slug="acme", email="da@acme.test", roles=[Role.DATA_ADMIN]
            ),
        )
    )
    body = {
        "name": "ERP MySQL",
        "connector_type": "mysql",
        "host": "db.example.com",
        "read_replica_host": "replica.example.com",
        "read_replica_port": 3307,
        "database_name": "erp",
        "username": "reader",
        "tls_mode": "verify-full",
    }
    r = await client.post("/api/v1/connections", json=body, headers=admin)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["connector_type"] == "mysql"
    assert out["read_replica_host"] == "replica.example.com" and out["read_replica_port"] == 3307
    bad = await client.post(
        "/api/v1/connections",
        json={**body, "name": "bad", "read_replica_host": "169.254.169.254"},
        headers=admin,
    )
    assert bad.status_code == 422


@pytest.mark.parametrize(
    "message",
    [
        'PostgreSQL server at "db:5433" rejected SSL upgrade',
        "The server does not support SSL connections",
    ],
)
def test_refused_tls_is_a_tls_error_not_an_unreachable_host(message: str) -> None:
    from app.connectors.common.sqlalchemy_adapter import classify_error

    assert classify_error(ConnectionError(message)).value == "TLS_ERROR"
    assert classify_error(ConnectionRefusedError("refused")).value == "HOST_UNREACHABLE"


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (
            asyncpg.InvalidPasswordError('password authentication failed for user "reader"'),
            ConnectorErrorCode.AUTH_FAILED,
        ),
        (
            asyncpg.InvalidCatalogNameError('database "erp" does not exist'),
            ConnectorErrorCode.DATABASE_NOT_FOUND,
        ),
    ],
)
async def test_postgres_connect_errors_are_classified_not_unhandled(
    monkeypatch: pytest.MonkeyPatch, driver_error: Exception, expected: ConnectorErrorCode
) -> None:
    """SQLAlchemy's asyncpg dialect re-raises asyncpg's own exception when *connecting*
    (only statement errors are translated); a wrong password used to escape as a 500."""

    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise driver_error

    monkeypatch.setattr(asyncpg, "connect", refuse)
    adapter = PostgreSQLAdapter(config(tls_mode=TlsMode.DISABLE, breaker_failure_threshold=1))
    try:
        result = await adapter.test_connection()
        assert result.ok is False
        assert result.error_code == expected.value

        with pytest.raises(ConnectorError) as info:
            await adapter.get_schemas()
        assert info.value.details["error_code"] == expected.value
        # The database answered, so the breaker must stay closed.
        with pytest.raises(ConnectorError):
            await adapter.execute_readonly(select(func.count()).select_from(table("orders")))
        assert adapter.breaker.state is CircuitState.CLOSED
    finally:
        await adapter.close()
