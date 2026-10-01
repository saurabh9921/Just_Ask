"""Adapter construction and per-connection adapter caching.

``AdapterCache`` keeps one adapter (and therefore one connection pool) per
database connection, keyed by a *version* (the connection row's ``updated_at``).
Updating a connection changes the version, so the stale pool is disposed and a
new one is built with the new settings/secret.
"""

from __future__ import annotations

import asyncio
import uuid

from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import ConnectionConfigError, UnsupportedConnectorError
from app.connectors.common.types import ConnectionConfig, ConnectorType
from app.connectors.registry import get_spec
from app.connectors.sqlite.adapter import SQLiteAdapter
from app.connectors.sqlserver.adapter import SQLServerAdapter
from app.core.logging import get_logger

logger = get_logger(__name__)


class ConnectorFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create(self, config: ConnectionConfig) -> DatabaseAdapter:
        spec = get_spec(
            config.connector_type, allow_foundation=self._settings.enable_foundation_connectors
        )
        if spec is None:
            raise UnsupportedConnectorError(details={"connector_type": config.connector_type.value})
        if config.connector_type is ConnectorType.SQLITE:
            if not self._settings.sqlite_allowed:
                raise ConnectionConfigError(
                    "SQLite connections are disabled in this environment.",
                    details={"reason": "SQLITE_DISABLED"},
                )
            return SQLiteAdapter(config, data_dir=self._settings.sqlite_data_dir)
        if config.connector_type is ConnectorType.SQLSERVER:
            return SQLServerAdapter(config, odbc_driver=self._settings.sqlserver_odbc_driver)
        return spec.adapter(config)


class AdapterCache:
    def __init__(self, factory: ConnectorFactory) -> None:
        self._factory = factory
        self._adapters: dict[uuid.UUID, tuple[str, DatabaseAdapter]] = {}
        self._lock = asyncio.Lock()

    async def get(self, config: ConnectionConfig, version: str) -> DatabaseAdapter:
        async with self._lock:
            cached = self._adapters.get(config.connection_id)
            if cached is not None and cached[0] == version:
                return cached[1]
            adapter = self._factory.create(config)
            self._adapters[config.connection_id] = (version, adapter)
        if cached is not None:
            await cached[1].close()
        return adapter

    async def evict(self, connection_id: uuid.UUID) -> None:
        async with self._lock:
            cached = self._adapters.pop(connection_id, None)
        if cached is not None:
            await cached[1].close()

    async def close_all(self) -> None:
        async with self._lock:
            adapters = [a for _, a in self._adapters.values()]
            self._adapters.clear()
        for adapter in adapters:
            try:
                await adapter.close()
            except Exception:  # shutdown must continue
                logger.warning("failed to close adapter", exc_info=True)
