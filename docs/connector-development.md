# Connector development

A connector lets ERP Insight AI read metadata from, and run governed read-only
queries against, one database engine. Connectors live in
`apps/api/app/connectors/<engine>/adapter.py`.

## Status

| Engine | Status | Driver | Maturity |
|---|---|---|---|
| PostgreSQL | **Implemented** (Phase 2) | asyncpg | GA |
| SQLite | **Implemented** (Phase 2) — development/tests only, disabled in production | aiosqlite | GA (dev) |
| MySQL 8 / MariaDB 10.6+ | **Implemented** (Phase 6) | aiomysql (PyMySQL) | GA |
| SQL Server 2017+ / Azure SQL | **Implemented** (Phase 6) | aioodbc + pyodbc + Microsoft ODBC Driver 18 (17 works) | GA |
| Oracle 19c+ | **Foundation** (Phase 6) — compiles/validates, not verified live | python-oracledb (thin, async) — extra `oracle` | foundation |
| SAP HANA | **Foundation** (Phase 6) — compile only, execution raises | sqlalchemy-hana (+ hdbcli) — extra `hana` | foundation |
| Snowflake | Planned stub (`app/connectors/snowflake`) | snowflake-sqlalchemy | — |
| BigQuery | Planned stub (`app/connectors/bigquery`) | sqlalchemy-bigquery | — |

- Creating a connection of a type without an adapter returns `422 CONNECTOR_NOT_SUPPORTED`.
- **Foundation** connectors are rejected with the same error unless
  `ENABLE_FOUNDATION_CONNECTORS=true`. Enable them only for evaluation; they have not
  had a live contract run.
- `registry.supported_types()` lists GA types; `get_spec(type, allow_foundation=...)`
  gates the rest.

## The contract (`app/connectors/base.py`)

| Method | Must |
|---|---|
| `test_connection()` | connect with the configured timeout, verify read-only posture, return `ConnectionTestResult` (never raise for DB-side failures) |
| `get_schemas / get_tables / get_views / get_columns / get_primary_keys / get_foreign_keys` | return structure only; hide system schemas and internal tables |
| `get_catalog(schemas)` | bulk reflection for metadata sync (one round per schema, not per table) |
| `execute_readonly(select, max_rows, timeout_seconds, query_id)` | accept only `sqlalchemy.Select`; reject `FOR UPDATE`; cap rows at `min(requested, config.max_rows)`; flag truncation; enforce timeout; honour cancellation |
| `stream_readonly(...)` | yield bounded batches without materialising the result |
| `explain_query(select)` | engine EXPLAIN (never ANALYZE) |
| `cancel_query(query_id)` | stop the running statement server-side |
| `normalize_type(raw)` | map to `CanonicalType` |
| `quote_identifier(name)` | always quote and escape |
| `close()` | dispose the pool |
| `compile_query_plan(plan)` | validated `ResolvedPlan` → dialect `CompiledQuery` (SQLAlchemy `Select` + output columns); SQLAlchemy adapters inherit it |
| `dialect` | the SQLAlchemy `Dialect` used to render SQL for display and validation |

Most SQLAlchemy-backed engines subclass `SqlAlchemyAdapter`
(`app/connectors/common/sqlalchemy_adapter.py`) and override hooks only:
`_create_engine(cfg)` (called for the primary and, if configured, the read replica),
`_begin_readonly`, `_verify_read_only`, `_server_version`, `_row_estimates`,
`_interrupt`, `explain_prefix`, `system_schemas`, `_parse_explain`. The base class
provides the per-connection concurrency limit, circuit breaker, replica routing,
timeouts, truncation detection, streaming (`stream_readonly(..., ceiling=)`) and
driver error classification (`_driver_code`: MySQL errno, ODBC SQLSTATE, `ORA-` codes).

## Adding a connector — checklist

1. **Driver**: add it to `apps/api/pyproject.toml` (prefer async drivers; if only sync
   exists, run it in a thread pool with a bounded executor).
2. **Adapter**: `app/connectors/<engine>/adapter.py`:
   - Build the URL with `sqlalchemy.URL.create(...)` — never string formatting.
   - Pool: `pool_size=config.pool_max_size`, `max_overflow=0`, `pool_pre_ping=True`.
   - Session read-only settings (examples): MySQL `SET SESSION TRANSACTION READ ONLY` +
     `max_execution_time`; SQL Server `ApplicationIntent=ReadOnly` + `SET LOCK_TIMEOUT`,
     query timeout via driver; Oracle `SET TRANSACTION READ ONLY`; Snowflake role with
     USAGE/SELECT only + `STATEMENT_TIMEOUT_IN_SECONDS`; BigQuery `maximum_bytes_billed`.
   - TLS: map `TlsMode` to the driver; `verify-full` must verify chain and hostname.
   - `_verify_read_only`: refuse admin-equivalent roles (`PostureError(ROLE_TOO_PRIVILEGED)`).
3. **Register** it in `app/connectors/registry.py` (`ConnectorSpec`: adapter class,
   `network`, `default_port`, display name). Add its type to `CONNECTOR_TYPES` if new.
4. **Contract**: add a backend branch to
   `apps/api/tests/contract/test_database_adapter_contract.py` that provisions the demo
   schema (`ERP_DDL`) and a dedicated SELECT-only role, then connects *as that role*.
   Do not weaken any assertion — in particular the raw-write rejection, timeout and
   cancellation tests.
5. **Integration fixture**: a Docker image in compose (profile) or documented external
   setup; tests skip when unavailable.
6. **Error mapping**: extend `_ERROR_NAME_MAP` with the driver's exception class names and
   `_driver_code` with its error numbers. Classify correctly: only unreachable-host and
   timeout errors trip the circuit breaker; permission and syntax errors must not.
7. **Dialect**: add time buckets to `query/compiler.py::time_bucket` (return the bucket's
   first day as a DATE; constants via `literal_column` so `GROUP BY` matches on engines
   that compare expressions textually, e.g. SQL Server), the sqlglot dialect to
   `sql_validator.DIALECTS`, and narrowly extend `ALLOWED_FUNCTIONS`/`ALLOWED_ANONYMOUS`
   if the buckets need new pure functions. `tests/unit/test_query_engine.py` must pass
   for the dialect (every example compiles *and* validates).
8. **Docs**: add the required database-side grants below.

## Customer-side provisioning

### PostgreSQL

```sql
CREATE ROLE erp_insight_ro LOGIN PASSWORD '<generated>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE erp_insight_ro SET default_transaction_read_only = on;
ALTER ROLE erp_insight_ro SET statement_timeout = '30s';
GRANT CONNECT ON DATABASE erp TO erp_insight_ro;
GRANT USAGE ON SCHEMA analytics TO erp_insight_ro;              -- reporting views only
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO erp_insight_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO erp_insight_ro;
```

Point the connection's `schema_name` at `analytics` and prefer a read replica host.
Then onboard it: `python scripts/dev.py onboard ... connect` (see the README).

### SQLite (development only)

Place the file inside `SQLITE_DATA_DIR` and create the connection with
`database_name` relative to that directory. Disabled in production.

### MySQL / MariaDB

```sql
CREATE USER 'erp_insight_ro'@'%' IDENTIFIED BY '<generated>' REQUIRE SSL;
GRANT SELECT, SHOW VIEW ON erp_reporting.* TO 'erp_insight_ro'@'%';   -- reporting schema only
ALTER USER 'erp_insight_ro'@'%' WITH MAX_USER_CONNECTIONS 10;         -- optional cap
```

- In MySQL a schema *is* a database: set `database_name` (and optionally `schema_name`)
  to the reporting database; discovery is limited to it.
- `test_connection` **refuses** accounts with global `ALL PRIVILEGES`, `SUPER` or
  write/DDL grants on `*.*`, and **warns** about write grants on the target database.
- Sessions run `SET SESSION TRANSACTION READ ONLY`; each statement gets
  `max_execution_time` (MySQL) / `max_statement_time` (MariaDB). Timeouts and
  cancellations issue `KILL QUERY <own connection id>` (no extra privilege needed).
- TLS: `verify-full` → CA + hostname check; `require` → encrypted, no verification
  (refused in production unless `ALLOW_INSECURE_CONNECTION_TLS`).
- Cost estimation uses `EXPLAIN FORMAT=JSON` (`query_cost`, rows examined).

### SQL Server / Azure SQL

```sql
CREATE LOGIN erp_insight_ro WITH PASSWORD = '<generated>', CHECK_POLICY = ON;
USE ErpReporting;
CREATE USER erp_insight_ro FOR LOGIN erp_insight_ro;
ALTER ROLE db_datareader ADD MEMBER erp_insight_ro;     -- or GRANT SELECT ON SCHEMA::reporting
DENY EXECUTE TO erp_insight_ro;                          -- no stored procedures
```

- SQL Server has no session read-only switch, so the posture check is strict:
  `sysadmin`, `db_owner`, `db_datawriter`, `db_ddladmin` or database-level
  INSERT/UPDATE/DELETE are **refused**; `CREATE TABLE` warns. Object-level write grants
  are not detected — grant `db_datareader` / schema SELECT only.
- The connection string is built from validated fields (values are `{}`-escaped; `;{}=`
  in host/driver names is rejected) with `ApplicationIntent=ReadOnly` (routes to readable
  AG secondaries), `Encrypt=yes` + `TrustServerCertificate=no` for `verify-full`, a login
  timeout and the ODBC query timeout; `SET LOCK_TIMEOUT 5000` per transaction; analytical
  transactions are always rolled back.
- Install **Microsoft ODBC Driver 18** (the API/worker images do; build arg
  `INSTALL_MSODBC=0` removes it) and set `SQLSERVER_ODBC_DRIVER` if you use another name.
- `explain_query` is not supported (SHOWPLAN needs a separate batch and extra permission);
  cost falls back to `sys.partitions` row estimates.

### Oracle (foundation)

```sql
CREATE USER erp_insight_ro IDENTIFIED BY "<generated>" QUOTA 0 ON users;
GRANT CREATE SESSION TO erp_insight_ro;
GRANT SELECT ON reporting.sales_orders_v TO erp_insight_ro;   -- per reporting view
ALTER USER erp_insight_ro PROFILE erp_readonly_profile;       -- CPU_PER_CALL / SESSIONS_PER_USER
```

Refused: `DBA`, full export/import roles, `SYSDBA`, any `* ANY *` privilege other than
`SELECT/READ ANY`; `CREATE TABLE/VIEW/PROCEDURE` warns. `SET TRANSACTION READ ONLY` per
transaction, `call_timeout` per round trip, `tcps` when TLS is enabled; cost uses
`ALL_TABLES.NUM_ROWS`. Missing before GA: a live contract run (Oracle Free container),
cancellation verification and wallet-based TLS configuration.

### SAP HANA (foundation)

Only compilation is implemented (day/month/year buckets; week and quarter raise
`CONNECTOR_NOT_SUPPORTED`). Execution needs a thread-pooled `hdbcli` runner with
`SET 'STATEMENT_TIMEOUT'`, read-only transactions and a cancellation design that does not
require `ALTER SYSTEM CANCEL SESSION`. Planned role: a technical user with `SELECT` on
calculation/reporting views only, `encrypt=true`, `sslValidateCertificate=true`.

### Snowflake and BigQuery (planned stubs)

- **Snowflake**: key-pair auth; a role with `USAGE` on one warehouse/database/schema and
  `SELECT` on reporting views; `STATEMENT_TIMEOUT_IN_SECONDS`, `QUERY_TAG`, resource
  monitors for credit guardrails; `SYSTEM$CANCEL_QUERY` for cancellation.
- **BigQuery**: a service account with `roles/bigquery.dataViewer` on the reporting
  dataset and `roles/bigquery.jobUser`; `maximum_bytes_billed` and a job timeout on every
  query; dry-run for cost estimation; job cancellation by id.

Both are `ConnectorType` values without registry entries (`CONNECTOR_NOT_SUPPORTED`).

## Read replicas

Every network connector accepts `read_replica_host` / `read_replica_port`. Analytical
statements (`execute_readonly`, `stream_readonly`, `explain_query`) run on the replica;
`test_connection`, posture checks and metadata discovery use the primary. The replica is
subject to the same host policy (denylist, DNS resolution) and TLS settings.

## Contract and integration runs

`tests/contract/test_database_adapter_contract.py` always runs SQLite. PostgreSQL, MySQL
and SQL Server run when an **admin** URL is provided: the test creates a throwaway
database and a SELECT-only account, then tests *as that account*.

```bash
export TEST_POSTGRES_ADMIN_URL="postgresql+asyncpg://admin:pw@127.0.0.1:5432/postgres"
export TEST_MYSQL_ADMIN_URL="mysql+aiomysql://root:pw@127.0.0.1:3306/"
export TEST_MSSQL_ADMIN_URL="mssql+aioodbc://sa:pw@127.0.0.1:1433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
python scripts/dev.py test-integration
```

Use disposable local servers only (e.g. `mysql:8.4`,
`mcr.microsoft.com/mssql/server:2022-latest`). The contract suite seeds its own
throwaway schema; never point it at a production database.
