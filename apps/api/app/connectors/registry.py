"""Connector registry: which ``ConnectorType`` values have an adapter, and how mature it is.

* ``ga``         - supported: PostgreSQL, MySQL/MariaDB, SQL Server, SQLite (dev/demo).
* ``foundation`` - adapter code exists and compiles/validates plans, but has not been
  verified against a live server (Oracle) or cannot execute yet (SAP HANA). Creating
  such connections requires ``ENABLE_FOUNDATION_CONNECTORS=true``.
* not registered - planned stubs (Snowflake, BigQuery): ``CONNECTOR_NOT_SUPPORTED``.

See docs/connector-development.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.connectors.base import DatabaseAdapter
from app.connectors.common.types import ConnectorType
from app.connectors.mysql.adapter import DEFAULT_PORT as MYSQL_PORT
from app.connectors.mysql.adapter import MySQLAdapter
from app.connectors.oracle.adapter import DEFAULT_PORT as ORACLE_PORT
from app.connectors.oracle.adapter import OracleAdapter
from app.connectors.postgresql.adapter import DEFAULT_PORT as PG_PORT
from app.connectors.postgresql.adapter import PostgreSQLAdapter
from app.connectors.sap_hana.adapter import DEFAULT_PORT as HANA_PORT
from app.connectors.sap_hana.adapter import SapHanaAdapter
from app.connectors.sqlite.adapter import SQLiteAdapter
from app.connectors.sqlserver.adapter import DEFAULT_PORT as MSSQL_PORT
from app.connectors.sqlserver.adapter import SQLServerAdapter

Maturity = Literal["ga", "foundation"]


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    connector_type: ConnectorType
    adapter: type[DatabaseAdapter]
    network: bool  # False => file-based (no host/port)
    default_port: int | None
    display_name: str
    maturity: Maturity = "ga"


REGISTRY: dict[ConnectorType, ConnectorSpec] = {
    ConnectorType.POSTGRESQL: ConnectorSpec(
        ConnectorType.POSTGRESQL, PostgreSQLAdapter, True, PG_PORT, "PostgreSQL"
    ),
    ConnectorType.MYSQL: ConnectorSpec(
        ConnectorType.MYSQL, MySQLAdapter, True, MYSQL_PORT, "MySQL / MariaDB"
    ),
    ConnectorType.SQLSERVER: ConnectorSpec(
        ConnectorType.SQLSERVER, SQLServerAdapter, True, MSSQL_PORT, "Microsoft SQL Server"
    ),
    ConnectorType.SQLITE: ConnectorSpec(ConnectorType.SQLITE, SQLiteAdapter, False, None, "SQLite"),
    ConnectorType.ORACLE: ConnectorSpec(
        ConnectorType.ORACLE, OracleAdapter, True, ORACLE_PORT, "Oracle Database", "foundation"
    ),
    ConnectorType.SAP_HANA: ConnectorSpec(
        ConnectorType.SAP_HANA, SapHanaAdapter, True, HANA_PORT, "SAP HANA", "foundation"
    ),
}


def get_spec(
    connector_type: ConnectorType, *, allow_foundation: bool = True
) -> ConnectorSpec | None:
    spec = REGISTRY.get(connector_type)
    if spec is not None and spec.maturity == "foundation" and not allow_foundation:
        return None
    return spec


def supported_types(*, allow_foundation: bool = False) -> list[ConnectorType]:
    return sorted(
        (t for t, s in REGISTRY.items() if s.maturity == "ga" or allow_foundation),
        key=lambda t: t.value,
    )
