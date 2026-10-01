"""Connector errors.

Messages are deliberately generic: driver error text can contain hostnames,
usernames, SQL or data values and is never returned to clients. A stable
``error_code`` tells the user what category of problem occurred.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.core.exceptions import AppError


class ConnectorErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    HOST_UNREACHABLE = "HOST_UNREACHABLE"
    TIMEOUT = "TIMEOUT"
    TLS_ERROR = "TLS_ERROR"
    DATABASE_NOT_FOUND = "DATABASE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ROLE_TOO_PRIVILEGED = "ROLE_TOO_PRIVILEGED"
    NOT_READ_ONLY = "NOT_READ_ONLY"
    UNKNOWN = "UNKNOWN"


class ConnectorError(AppError):
    status_code: ClassVar[int] = 502
    code: ClassVar[str] = "CONNECTOR_ERROR"
    default_message: ClassVar[str] = "The database returned an error."


class ConnectorUnavailableError(ConnectorError):
    status_code = 503
    code = "CONNECTOR_UNAVAILABLE"
    default_message = "The database is currently unreachable."


class QueryTimeoutError(ConnectorError):
    status_code = 504
    code = "QUERY_TIMEOUT"
    default_message = "The query exceeded the allowed execution time."


class QueryCancelledError(ConnectorError):
    status_code = 409
    code = "QUERY_CANCELLED"
    default_message = "The query was cancelled."


class ConnectorCircuitOpenError(ConnectorUnavailableError):
    code = "CONNECTOR_CIRCUIT_OPEN"
    default_message = (
        "The database has failed repeatedly and is paused briefly. Please retry shortly."
    )


class ConnectorBusyError(ConnectorUnavailableError):
    code = "CONNECTOR_BUSY"
    default_message = "The database connection limit is reached. Please retry shortly."


class UnsupportedConnectorError(AppError):
    status_code = 422
    code = "CONNECTOR_NOT_SUPPORTED"
    default_message = "This database type is not supported yet."


class ConnectionConfigError(AppError):
    status_code = 422
    code = "INVALID_CONNECTION_CONFIG"
    default_message = "The connection configuration is not allowed."


class UnsafeStatementError(AppError):
    status_code = 403
    code = "QUERY_NOT_ALLOWED"
    default_message = "The requested operation is not permitted."
