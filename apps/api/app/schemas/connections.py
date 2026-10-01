"""Connection management models.

There is intentionally no ``read_only`` input field: every connection is read-only
and a request that tries to set it is rejected as an unknown field (422).
Secrets are write-only: responses never contain passwords or local secret refs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, StringConstraints, model_validator

from app.connectors.common.types import ConnectorType, TlsMode
from app.core.permissions import Role
from app.schemas.common import ResponseModel, StrictModel

ConnectionName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[\w .\-()]+$"
    ),
]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
HostName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]

DEFAULT_GRANTS: tuple[Role, ...] = (Role.TENANT_ADMIN, Role.DATA_ADMIN, Role.ANALYST, Role.VIEWER)


class _SecretFields(StrictModel):
    password: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    secret_ref: str | None = Field(default=None, max_length=512)
    tls_ca_pem: str | None = Field(default=None, max_length=65_536)

    @model_validator(mode="after")
    def _one_secret_source(self) -> Self:
        if self.password is not None and self.secret_ref is not None:
            raise ValueError("provide either password or secret_ref, not both")
        if self.tls_ca_pem is not None and "BEGIN CERTIFICATE" not in self.tls_ca_pem:
            raise ValueError("tls_ca_pem must be a PEM-encoded certificate")
        return self


class ConnectionCreate(_SecretFields):
    name: ConnectionName
    connector_type: ConnectorType
    host: HostName | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    read_replica_host: HostName | None = None
    read_replica_port: int | None = Field(default=None, ge=1, le=65535)
    database_name: Identifier | None = None
    schema_name: Identifier | None = None
    username: Identifier | None = None
    tls_mode: TlsMode = TlsMode.VERIFY_FULL
    pool_min_size: int = Field(default=1, ge=0, le=20)
    pool_max_size: int = Field(default=5, ge=1, le=50)
    query_timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    granted_roles: list[Role] = Field(default_factory=lambda: list(DEFAULT_GRANTS), max_length=5)

    @model_validator(mode="after")
    def _pool_bounds(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size must not exceed pool_max_size")
        return self


class ConnectionUpdate(_SecretFields):
    name: ConnectionName | None = None
    host: HostName | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    read_replica_host: HostName | None = None
    read_replica_port: int | None = Field(default=None, ge=1, le=65535)
    database_name: Identifier | None = None
    schema_name: Identifier | None = None
    username: Identifier | None = None
    tls_mode: TlsMode | None = None
    pool_min_size: int | None = Field(default=None, ge=0, le=20)
    pool_max_size: int | None = Field(default=None, ge=1, le=50)
    query_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_rows: int | None = Field(default=None, ge=1, le=100_000)
    granted_roles: list[Role] | None = Field(default=None, max_length=5)


class ConnectionOut(ResponseModel):
    id: uuid.UUID
    name: str
    connector_type: str
    host: str | None
    port: int | None
    read_replica_host: str | None = None
    read_replica_port: int | None = None
    database_name: str | None
    schema_name: str | None
    username: str | None
    tls_mode: str
    read_only: bool
    pool_min_size: int
    pool_max_size: int
    query_timeout_seconds: int
    max_rows: int
    status: str
    last_tested_at: datetime | None
    last_test_error_code: str | None
    has_secret: bool
    secret_source: Literal["encrypted", "env", "vault"] | None
    granted_roles: list[str]
    created_at: datetime
    updated_at: datetime


class ConnectionList(ResponseModel):
    items: list[ConnectionOut]
    total: int


class ConnectionTestOut(ResponseModel):
    ok: bool
    latency_ms: float | None
    server_version: str | None
    error_code: str | None
    message: str | None
    warnings: list[str] = Field(default_factory=list)
