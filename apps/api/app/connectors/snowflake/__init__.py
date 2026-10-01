"""Snowflake connector - PLANNED (stub).

Not implemented. Intended design (see docs/connector-development.md):
``snowflake-sqlalchemy`` dialect executed in worker threads (the connector is
synchronous), key-pair authentication with a read-only role limited to reporting
views, ``STATEMENT_TIMEOUT_IN_SECONDS`` and ``QUERY_TAG`` per session, warehouse size
and credit guardrails, and ``SYSTEM$CANCEL_QUERY`` for cancellation. The
``ConnectorType.SNOWFLAKE`` value exists so configurations can be validated, but the
registry has no adapter: creating such a connection returns CONNECTOR_NOT_SUPPORTED.
"""
