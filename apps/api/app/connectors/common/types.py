"""Connector-neutral types shared by every database adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ConnectorType(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    SQLITE = "sqlite"
    ORACLE = "oracle"
    SAP_HANA = "sap_hana"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"


class TlsMode(StrEnum):
    DISABLE = "disable"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


INSECURE_TLS_MODES = frozenset({TlsMode.DISABLE, TlsMode.PREFER})


class CanonicalType(StrEnum):
    """Database-independent column types used by the semantic layer and UI."""

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
    JSON = "json"
    BINARY = "binary"
    UUID = "uuid"
    OTHER = "other"


class ObjectKind(StrEnum):
    TABLE = "table"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Fully resolved configuration handed to an adapter. Holds the secret in memory only."""

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    connector_type: ConnectorType
    host: str | None
    port: int | None
    database: str | None
    schema: str | None
    username: str | None
    password: SecretStr | None = field(repr=False)
    tls_mode: TlsMode = TlsMode.VERIFY_FULL
    tls_ca_pem: SecretStr | None = field(default=None, repr=False)
    pool_min_size: int = 1
    pool_max_size: int = 5
    query_timeout_seconds: int = 30
    max_rows: int = 1000
    connect_timeout_seconds: float = 10.0
    # Phase 6 performance controls
    read_replica_host: str | None = None
    read_replica_port: int | None = None
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    queue_timeout_seconds: float = 10.0
    options: dict[str, Any] = field(default_factory=dict)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ColumnInfo(_Model):
    name: str
    data_type: str
    canonical_type: CanonicalType
    nullable: bool
    ordinal: int
    comment: str | None = None


class ForeignKeyInfo(_Model):
    name: str | None
    columns: list[str]
    referred_schema: str | None
    referred_table: str
    referred_columns: list[str]


class TableInfo(_Model):
    schema_name: str
    name: str
    kind: ObjectKind
    comment: str | None = None


class ObjectMetadata(_Model):
    """One table/view with its structure. No data values are ever included."""

    schema_name: str
    name: str
    kind: ObjectKind
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    row_estimate: int | None = None
    comment: str | None = None


class CatalogSnapshot(_Model):
    database: str | None
    server_version: str | None
    objects: list[ObjectMetadata]


class ConnectionTestResult(_Model):
    ok: bool
    latency_ms: float | None = None
    server_version: str | None = None
    error_code: str | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ResultColumn(_Model):
    name: str
    data_type: str | None = None


class QueryResult(_Model):
    columns: list[ResultColumn]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool
    elapsed_ms: float


class ExplainResult(_Model):
    plan: Any
    estimated_rows: float | None = None
    estimated_cost: float | None = None
