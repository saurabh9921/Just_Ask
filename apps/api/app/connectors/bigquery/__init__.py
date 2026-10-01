"""Google BigQuery connector - PLANNED (stub).

Not implemented. Intended design (see docs/connector-development.md): the
``sqlalchemy-bigquery`` dialect, a service account with ``roles/bigquery.dataViewer`` +
``roles/bigquery.jobUser`` on dedicated reporting datasets, ``maximum_bytes_billed`` as
the cost guardrail (dry-run jobs give exact byte estimates for the cost estimator),
job timeouts and ``jobs.cancel`` for cancellation. The ``ConnectorType.BIGQUERY`` value
exists, but the registry has no adapter: creating such a connection returns
CONNECTOR_NOT_SUPPORTED.
"""
