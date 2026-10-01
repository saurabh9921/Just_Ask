"""Database connection management (tenant-scoped) and adapter access.

Authorization model:
* ``connections.manage`` holders see and manage every live connection in their tenant.
* Everyone else (``connections.read``) sees only connections granted to one of their
  roles. Invisible connections produce 404 (existence is not disclosed).
* Tenant ID always comes from the authenticated context, never from the request.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import ConnectionConfigError, UnsupportedConnectorError
from app.connectors.common.network import HostPolicy
from app.connectors.common.types import (
    INSECURE_TLS_MODES,
    ConnectionConfig,
    ConnectionTestResult,
    ConnectorType,
    TlsMode,
)
from app.connectors.factory import AdapterCache
from app.connectors.registry import get_spec
from app.connectors.sqlite.adapter import resolve_sqlite_path
from app.core.exceptions import ConflictError, NotFoundError
from app.core.permissions import Permission, Role
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import ConnectionGrant, DatabaseConnection, Tenant
from app.db.models import Role as RoleModel
from app.schemas.connections import ConnectionCreate, ConnectionOut, ConnectionUpdate
from app.services.audit_service import AuditOutcome, AuditService
from app.services.secrets_service import SecretCipher, SecretStore

_CONFIG_FIELDS = (
    "connector_type",
    "host",
    "port",
    "read_replica_host",
    "read_replica_port",
    "database_name",
    "schema_name",
    "username",
    "secret_ref",
    "tls_mode",
    "tls_ca_ref",
    "pool_min_size",
    "pool_max_size",
    "query_timeout_seconds",
    "max_rows",
)


def config_version(conn: DatabaseConnection) -> str:
    """Fingerprint of everything that affects the connection pool."""
    payload = json.dumps({f: getattr(conn, f) for f in _CONFIG_FIELDS}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(slots=True)
class ConnectionService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    cipher: SecretCipher
    adapters: AdapterCache
    host_policy: HostPolicy
    audit: AuditService

    @property
    def _secrets(self) -> SecretStore:
        return SecretStore(self.session, self.cipher)

    # ---------------------------------------------------------------- reads
    async def _role_ids(self, roles: list[Role] | frozenset[Role]) -> dict[str, int]:
        rows = await self.session.execute(
            select(RoleModel.name, RoleModel.id).where(RoleModel.name.in_([r.value for r in roles]))
        )
        return {str(name): int(rid) for name, rid in rows}

    async def _visible_query_filter(self) -> list[object]:
        base: list[object] = [
            DatabaseConnection.tenant_id == self.ctx.tenant_id,
            DatabaseConnection.deleted_at.is_(None),
        ]
        if self.ctx.has(Permission.CONNECTIONS_MANAGE):
            return base
        role_ids = list((await self._role_ids(self.ctx.roles)).values())
        granted = select(ConnectionGrant.connection_id).where(
            ConnectionGrant.tenant_id == self.ctx.tenant_id, ConnectionGrant.role_id.in_(role_ids)
        )
        return [*base, DatabaseConnection.id.in_(granted)]

    async def list_visible(self) -> list[DatabaseConnection]:
        self.ctx.require(Permission.CONNECTIONS_READ)
        rows = await self.session.scalars(
            select(DatabaseConnection)
            .where(*await self._visible_query_filter())  # type: ignore[arg-type]
            .order_by(DatabaseConnection.name)
        )
        return list(rows)

    async def get(self, connection_id: uuid.UUID) -> DatabaseConnection:
        self.ctx.require(Permission.CONNECTIONS_READ)
        conn = await self.session.scalar(
            select(DatabaseConnection).where(
                DatabaseConnection.id == connection_id,
                *await self._visible_query_filter(),  # type: ignore[arg-type]
            )
        )
        if conn is None:
            raise NotFoundError()
        return conn

    async def granted_roles(self, connection_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
        rows = await self.session.execute(
            select(ConnectionGrant.connection_id, RoleModel.name)
            .join(RoleModel, RoleModel.id == ConnectionGrant.role_id)
            .where(
                ConnectionGrant.tenant_id == self.ctx.tenant_id,
                ConnectionGrant.connection_id.in_(connection_ids),
            )
            .order_by(RoleModel.id)
        )
        result: dict[uuid.UUID, list[str]] = {cid: [] for cid in connection_ids}
        for cid, name in rows:
            result[cid].append(name)
        return result

    @staticmethod
    def to_out(conn: DatabaseConnection, roles: list[str]) -> ConnectionOut:
        ref = conn.secret_ref or ""
        sources = {"local://": "encrypted", "env://": "env", "vault://": "vault"}
        source = next((v for k, v in sources.items() if ref.startswith(k)), None)
        return ConnectionOut(
            id=conn.id,
            name=conn.name,
            connector_type=conn.connector_type,
            host=conn.host,
            port=conn.port,
            read_replica_host=conn.read_replica_host,
            read_replica_port=conn.read_replica_port,
            database_name=conn.database_name,
            schema_name=conn.schema_name,
            username=conn.username,
            tls_mode=conn.tls_mode,
            read_only=conn.read_only,
            pool_min_size=conn.pool_min_size,
            pool_max_size=conn.pool_max_size,
            query_timeout_seconds=conn.query_timeout_seconds,
            max_rows=conn.max_rows,
            status=conn.status,
            last_tested_at=conn.last_tested_at,
            last_test_error_code=conn.last_test_error_code,
            has_secret=bool(conn.secret_ref),
            secret_source=source,  # type: ignore[arg-type]
            granted_roles=roles,
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    # --------------------------------------------------------------- writes
    def _validate_shape(self, conn: DatabaseConnection) -> None:
        ctype = ConnectorType(conn.connector_type)
        spec = get_spec(ctype, allow_foundation=self.settings.enable_foundation_connectors)
        if spec is None:
            raise UnsupportedConnectorError(details={"connector_type": ctype.value})
        if conn.max_rows > self.settings.max_query_rows:
            raise ConnectionConfigError(
                f"max_rows cannot exceed the platform limit ({self.settings.max_query_rows}).",
                details={"reason": "MAX_ROWS_TOO_HIGH"},
            )
        if conn.query_timeout_seconds > self.settings.max_query_seconds:
            raise ConnectionConfigError(
                f"query_timeout_seconds cannot exceed {self.settings.max_query_seconds}.",
                details={"reason": "TIMEOUT_TOO_HIGH"},
            )
        if not spec.network:
            if ctype is ConnectorType.SQLITE and not self.settings.sqlite_allowed:
                raise ConnectionConfigError(
                    "SQLite connections are disabled in this environment.",
                    details={"reason": "SQLITE_DISABLED"},
                )
            resolve_sqlite_path(conn.database_name, self.settings.sqlite_data_dir)
            if conn.host or conn.username or conn.secret_ref:
                raise ConnectionConfigError(
                    "File-based connections take no host, username or secret.",
                    details={"reason": "UNEXPECTED_FIELDS"},
                )
            return
        if not conn.host or not conn.database_name or not conn.username:
            raise ConnectionConfigError(
                "host, database_name and username are required.",
                details={"reason": "MISSING_FIELDS"},
            )
        self.host_policy.check_syntax(conn.host)
        if conn.read_replica_host:
            self.host_policy.check_syntax(conn.read_replica_host)
        elif conn.read_replica_port:
            raise ConnectionConfigError(
                "read_replica_port needs read_replica_host.",
                details={"reason": "MISSING_FIELDS"},
            )
        if (
            TlsMode(conn.tls_mode) in INSECURE_TLS_MODES
            and not self.settings.insecure_connection_tls_allowed
        ):
            raise ConnectionConfigError(
                "TLS is required for database connections in this environment.",
                details={"reason": "TLS_REQUIRED"},
            )

    async def _set_grants(self, conn: DatabaseConnection, roles: list[Role]) -> None:
        await self.session.execute(
            delete(ConnectionGrant).where(ConnectionGrant.connection_id == conn.id)
        )
        role_ids = await self._role_ids(roles)
        for role in dict.fromkeys(roles):
            self.session.add(
                ConnectionGrant(
                    connection_id=conn.id, role_id=role_ids[role.value], tenant_id=conn.tenant_id
                )
            )

    async def _apply_secrets(
        self, conn: DatabaseConnection, body: ConnectionCreate | ConnectionUpdate
    ) -> None:
        tenant_id = self.ctx.tenant_id
        if body.password is not None:
            old = conn.secret_ref
            conn.secret_ref = await self._secrets.store(
                tenant_id=tenant_id, purpose="db_password", value=body.password
            )
            await self._secrets.delete_if_local(tenant_id=tenant_id, ref=old)
        elif body.secret_ref is not None:
            old = conn.secret_ref
            conn.secret_ref = self._secrets.validate_external_ref(
                body.secret_ref, tenant_slug=self.ctx.tenant_slug
            )
            await self._secrets.delete_if_local(tenant_id=tenant_id, ref=old)
        if body.tls_ca_pem is not None:
            from pydantic import SecretStr  # noqa: PLC0415 - local to the rare CA path

            old = conn.tls_ca_ref
            conn.tls_ca_ref = await self._secrets.store(
                tenant_id=tenant_id, purpose="tls_ca", value=SecretStr(body.tls_ca_pem)
            )
            await self._secrets.delete_if_local(tenant_id=tenant_id, ref=old)

    async def create(self, body: ConnectionCreate) -> DatabaseConnection:
        self.ctx.require(Permission.CONNECTIONS_MANAGE)
        conn = DatabaseConnection(
            id=uuid.uuid4(),
            tenant_id=self.ctx.tenant_id,
            name=body.name,
            connector_type=body.connector_type.value,
            host=body.host,
            port=body.port,
            read_replica_host=body.read_replica_host,
            read_replica_port=body.read_replica_port,
            database_name=body.database_name,
            schema_name=body.schema_name,
            username=body.username,
            tls_mode=body.tls_mode.value,
            pool_min_size=body.pool_min_size,
            pool_max_size=body.pool_max_size,
            query_timeout_seconds=body.query_timeout_seconds,
            max_rows=body.max_rows,
            read_only=True,
            status="untested",
            options={},
            created_by=self.ctx.user_id,
        )
        self._validate_shape(conn)
        self.session.add(conn)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("A connection with this name already exists.") from exc
        await self._apply_secrets(conn, body)
        await self._set_grants(conn, body.granted_roles)
        await self.session.commit()
        await self._audit("connection.create", conn, {"connector_type": conn.connector_type})
        return conn

    async def update(self, connection_id: uuid.UUID, body: ConnectionUpdate) -> DatabaseConnection:
        self.ctx.require(Permission.CONNECTIONS_MANAGE)
        conn = await self.get(connection_id)
        changes = body.model_dump(
            exclude_unset=True,
            exclude={"password", "secret_ref", "tls_ca_pem", "granted_roles"},
        )
        for key, value in changes.items():
            if value is None and key in {"name", "tls_mode"}:
                continue
            setattr(conn, key, value.value if isinstance(value, TlsMode) else value)
        if conn.pool_min_size > conn.pool_max_size:
            raise ConnectionConfigError(
                "pool_min_size must not exceed pool_max_size.", details={"reason": "POOL_RANGE"}
            )
        self._validate_shape(conn)
        await self._apply_secrets(conn, body)
        if body.granted_roles is not None:
            await self._set_grants(conn, body.granted_roles)
        conn.status = "untested"
        conn.last_test_error_code = None
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("A connection with this name already exists.") from exc
        await self.adapters.evict(conn.id)
        changed = sorted(
            {
                *changes,
                *(
                    k
                    for k in ("password", "secret_ref", "tls_ca_pem", "granted_roles")
                    if getattr(body, k) is not None
                ),
            }
        )
        await self._audit("connection.update", conn, {"fields": changed})
        return conn

    async def delete(self, connection_id: uuid.UUID) -> None:
        self.ctx.require(Permission.CONNECTIONS_MANAGE)
        conn = await self.get(connection_id)
        conn.deleted_at = utcnow()
        await self._secrets.delete_if_local(tenant_id=self.ctx.tenant_id, ref=conn.secret_ref)
        await self._secrets.delete_if_local(tenant_id=self.ctx.tenant_id, ref=conn.tls_ca_ref)
        conn.secret_ref = None
        conn.tls_ca_ref = None
        await self.session.execute(
            delete(ConnectionGrant).where(ConnectionGrant.connection_id == conn.id)
        )
        await self.session.commit()
        await self.adapters.evict(conn.id)
        await self._audit("connection.delete", conn, {})

    # ------------------------------------------------------------- adapters
    async def adapter_for(self, conn: DatabaseConnection) -> DatabaseAdapter:
        return await adapter_for_connection(
            session=self.session,
            settings=self.settings,
            cipher=self.cipher,
            adapters=self.adapters,
            host_policy=self.host_policy,
            conn=conn,
            tenant_slug=self.ctx.tenant_slug,
        )

    async def test(self, connection_id: uuid.UUID) -> ConnectionTestResult:
        conn = await self.get(connection_id)
        adapter = await self.adapter_for(conn)
        result = await adapter.test_connection()
        conn.status = "ok" if result.ok else "error"
        conn.last_tested_at = utcnow()
        conn.last_test_error_code = result.error_code
        await self.session.commit()
        await self._audit(
            "connection.test",
            conn,
            {"ok": result.ok, "error_code": result.error_code},
            outcome=AuditOutcome.SUCCESS if result.ok else AuditOutcome.ERROR,
        )
        return result

    async def _audit(
        self,
        event: str,
        conn: DatabaseConnection,
        details: dict[str, object],
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
    ) -> None:
        await self.audit.record(
            event,
            outcome,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="connection",
            resource_id=str(conn.id),
            details=details,
        )


async def adapter_for_connection(
    *,
    session: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
    adapters: AdapterCache,
    host_policy: HostPolicy,
    conn: DatabaseConnection,
    tenant_slug: str | None = None,
) -> DatabaseAdapter:
    """Resolve secrets, enforce host policy (with DNS) and return the cached adapter."""
    if tenant_slug is None:
        tenant_slug = (
            await session.scalar(select(Tenant.slug).where(Tenant.id == conn.tenant_id))
        ) or ""
    ctype = ConnectorType(conn.connector_type)
    spec = get_spec(ctype, allow_foundation=settings.enable_foundation_connectors)
    if spec is None:
        raise UnsupportedConnectorError(details={"connector_type": ctype.value})
    if spec.network and conn.host:
        await host_policy.check(conn.host, conn.port or spec.default_port)
    if spec.network and conn.read_replica_host:
        await host_policy.check(
            conn.read_replica_host, conn.read_replica_port or conn.port or spec.default_port
        )
    store = SecretStore(session, cipher)
    config = ConnectionConfig(
        connection_id=conn.id,
        tenant_id=conn.tenant_id,
        connector_type=ctype,
        host=conn.host,
        port=conn.port or spec.default_port,
        database=conn.database_name,
        schema=conn.schema_name,
        username=conn.username,
        password=await store.resolve(
            tenant_id=conn.tenant_id, tenant_slug=tenant_slug, ref=conn.secret_ref
        ),
        tls_mode=TlsMode(conn.tls_mode),
        tls_ca_pem=await store.resolve(
            tenant_id=conn.tenant_id, tenant_slug=tenant_slug, ref=conn.tls_ca_ref
        ),
        pool_min_size=conn.pool_min_size,
        pool_max_size=conn.pool_max_size,
        query_timeout_seconds=conn.query_timeout_seconds,
        max_rows=conn.max_rows,
        connect_timeout_seconds=settings.connector_connect_timeout_seconds,
        read_replica_host=conn.read_replica_host,
        read_replica_port=conn.read_replica_port,
        breaker_failure_threshold=settings.connector_breaker_failures,
        breaker_reset_seconds=settings.connector_breaker_reset_seconds,
        queue_timeout_seconds=settings.connector_queue_timeout_seconds,
    )
    return await adapters.get(config, config_version(conn))
