# ERP Insight AI — Performance and scalability (Phase 6)

ERP databases are shared, business-critical systems. The platform's first performance
duty is to **protect the ERP** (bounded, short, read-only statements on replicas or
reporting views); its second is to answer quickly.

## 1. Targets

| Path | Target | How it is met | Checked by |
|---|---|---|---|
| Cached metadata / semantic catalog | < 100 ms | in-process `CatalogCache` (LRU, keyed by tenant + snapshot + bundle hash) + Redis retrieval cache | `tests/unit/test_result_cache.py` |
| Cached common question | < 500 ms | Redis query-result cache (section 3) skips compile/execute | `tests/performance/test_budgets.py::test_cached_question_is_fast` |
| Plan → SQL → validation | < 50 ms p95 per dialect | deterministic compiler + AST validator, no I/O | `test_plan_pipeline_latency` |
| Normal analytical query | < 5 s | reporting views, mandatory date windows on large facts, row cap, `MAX_QUERY_SECONDS`, cost check | integration (DB dependent) |
| Extension first response | < 300 ms | WebSocket `accepted` frame before planning starts | Phase 5 tests |
| Progress events | < 1 s | one event per state-machine step | Phase 5 tests |
| Export memory | constant in the row count | streamed batches, incremental writer | `test_export_writer_memory_is_bounded` (200k rows < 4 MiB peak) |

Run the budgets: `python scripts/dev.py test-performance` (no Docker needed; part of
`check`). Budgets are generous on purpose so they hold on shared CI runners: they catch
order-of-magnitude regressions, they are not benchmarks. Real latency depends on the
customer database and the model; measure with `erp_analytics_query_duration_seconds`,
`erp_agent_step_duration_seconds` and connector latency histograms.

## 2. Protecting the ERP database

| Control | Setting | Behaviour |
|---|---|---|
| Read replica | connection `read_replica_host` / `read_replica_port` | analytical statements run on the replica; metadata discovery and posture checks stay on the primary. The replica host passes the same SSRF policy as the primary (literal + DNS). |
| Pool per connection | `pool_min_size`/`pool_max_size` (connection) | `max_overflow=0`, `pool_pre_ping` |
| Concurrency limit per connection | semaphore = `pool_max_size` | waiters queue up to `CONNECTOR_QUEUE_TIMEOUT_SECONDS` (10 s), then `503 CONNECTOR_BUSY` |
| Circuit breaker per connection | `CONNECTOR_BREAKER_FAILURES` (5), `CONNECTOR_BREAKER_RESET_SECONDS` (30) | only *unhealthy-database* failures count (unreachable host, timeouts). Permission errors, bad SQL and user cancellations do not. When open: `503 CONNECTOR_CIRCUIT_OPEN` without touching the DB; after the reset period one probe is let through (half-open). |
| Statement timeout | `MAX_QUERY_SECONDS` / connection `query_timeout_seconds` | enforced server-side (PostgreSQL `statement_timeout`, MySQL `max_execution_time`, MariaDB `max_statement_time`, SQL Server driver query timeout + `LOCK_TIMEOUT`, Oracle `call_timeout`) and client-side; the server statement is cancelled (`pg_cancel_backend`, `KILL QUERY`, driver cancel) |
| Row cap | `MAX_QUERY_ROWS`, connection `max_rows` | `LIMIT n+1` to detect truncation; streaming never materialises more |
| Cost check | EXPLAIN (PostgreSQL, MySQL) | `QUERY_TOO_EXPENSIVE` above the configured thresholds; unknown cost never blocks |
| Rate limits | `QUERY_RATE_LIMIT_PER_USER_PER_MINUTE` (30), `QUERY_RATE_LIMIT_PER_TENANT_PER_MINUTE` (600) | per user *and* per tenant (one tenant's users cannot starve others); `429 RATE_LIMITED` + `Retry-After` |

## 3. Caching

| Cache | Where | Key | TTL / invalidation |
|---|---|---|---|
| Query result | Redis `qr:{tenant}:{connection}:{config_version}:{semantic_version}:{scope_hash}:{plan_hash}:{sql_hash}` | tenant, connection, connection-config version, semantic version, **security scope** (permissions + row-filter values), normalised resolved plan (includes the concrete date window), compiled SQL | `QUERY_CACHE_TTL_SECONDS` (300 s). Editing the connection changes `config_version`; re-ingesting the semantic layer changes `semantic_version`; relative periods resolve to concrete dates, so "last 30 days" tomorrow is a different key. |
| Semantic catalog + bindings | process memory (`CatalogCache`, `CATALOG_CACHE_SIZE`=64) | tenant, metadata snapshot id, semantic bundle hash | LRU; a new snapshot or bundle is a new key |
| Retrieval | Redis `rag:…` (Phase 3) | tenant, connection, permission hash, semantic version, query hash | `RETRIEVAL_CACHE_TTL_SECONDS`; invalidated when feedback memory changes |
| Metadata | Redis `meta:…` (Phase 2) | tenant, connection, version | `METADATA_CACHE_TTL_SECONDS` |

Rules (enforced in code and by `tests/security/test_phase6_security.py`):
- Every key contains the tenant **and** the authorization scope. Two users share a cached
  answer only when their permissions and row-filter values are identical — the same
  answer they would get by executing.
- A cache hit is still a fully authorized request: authentication, rate limits, plan
  validation and SQL validation run before the cache is consulted; only execution is
  skipped. The hit is audited (`cache_hit: true`) and shown in the answer
  (`cache: {hit, age_seconds}`; the extension shows "cached result, N min old").
- Redis failures fail open to a cache miss (the query executes); a cache never serves
  data across tenants, and cached entries carry no credentials.
- Disable with `QUERY_RESULT_CACHE_ENABLED=false` (e.g. for real-time dashboards).

## 4. Materialized-view strategy

The platform never creates objects in customer databases (read-only by design), so
materialization is a **customer-side, governed** decision:

1. **Start with reporting views** (`analytics_*`) that pre-join and pre-filter the ERP
   tables; the semantic layer points at them (`source.allowed_database_objects`).
2. **Promote hot, heavy views to materialized views / summary tables** when the metrics
   show them: high `erp_analytics_query_duration_seconds` for an entity, frequent
   `QUERY_TOO_EXPENSIVE`, or DBA reports. Typical candidates: daily sales by
   customer/product/region, inventory snapshots, AR/AP ageing.
   - PostgreSQL: `CREATE MATERIALIZED VIEW … ; REFRESH MATERIALIZED VIEW CONCURRENTLY` on a
     schedule (needs a unique index); Oracle: materialized views with `REFRESH FAST ON
     DEMAND`; SQL Server: indexed views or ETL summary tables; MySQL/MariaDB: summary
     tables refreshed by events or ETL.
3. **Model the aggregate as its own semantic entity** (e.g. `DailySales`) with
   `time_fields` at the aggregate's grain and a **freshness note** in its description;
   keep the detailed entity for drill-down. Add examples so the planner prefers the
   aggregate for grain ≥ day.
4. **Grant SELECT on the materialized object** to the read-only role; nothing else changes.
5. **Freshness**: `data_freshness` in answers is the semantic index time; state the
   refresh schedule in the entity description until per-entity freshness metadata lands
   (Phase 7).

Partitioning: large facts should be partitioned by the time field used in
`time_fields`; the mandatory date window on large entities (`large_fact: true`) keeps
queries partition-pruned.

## 5. Background exports

`POST /api/v1/exports` → 202 → Celery `exports` queue (`export.run`) → file → one-time
download link. Design choices for scale:

- **Streaming end to end**: `stream_readonly` yields batches (`EXPORT_BATCH_SIZE`, 2000)
  from a server-side cursor; `ExportWriter` writes each batch to disk immediately (CSV or
  streaming JSON). Memory stays flat regardless of the row count (tested with 200k rows).
- **Bounded**: `EXPORT_MAX_ROWS` (100k; `truncated` is reported), `EXPORT_TIMEOUT_SECONDS`
  (600) plus Celery soft/hard limits (900/960 s), `EXPORT_MAX_ACTIVE_PER_USER` (2).
- **Temporary storage with expiry**: files live in `EXPORT_DIR/<tenant_id>/` for
  `EXPORT_TTL_SECONDS` (1 h) after completion; the `scheduler` service (Celery beat) runs
  `export.purge` every 5 minutes and marks jobs `expired`. Downloads after expiry → `410`.
- **Shared volume**: the worker writes and the API serves, so both mount `exports-data`
  (compose). Multi-host deployments need a shared ReadWriteMany volume or object storage
  (a storage backend with presigned URLs is a Phase 7 option).
- **Sensitive scan before download**: every string cell is checked for payment-card
  numbers (Luhn) and IBANs (mod-97); a match aborts the job (`SENSITIVE_DATA_DETECTED`)
  and deletes the partial file, so nothing that looks like one is ever downloadable.
- Exports re-run the stored, validated plan under the *current* semantic version and
  the user's *current* permissions (not a replay of stale authorization).

## 6. Connection pools and workers

- API: one pool per connection per process (`pool_max_size`); size replicas × pool so the
  ERP's connection budget is respected (`API_REPLICAS × pool_max_size + WORKER_REPLICAS ×
  pool_max_size` ≤ the role's connection limit, e.g. PostgreSQL `ALTER ROLE … CONNECTION LIMIT`).
- Worker: `--concurrency 4` per replica; exports are I/O bound and time-limited.
- Idle adapters are closed by the registry when a connection is edited or deleted.

## 7. Known limits

- Budgets are verified on SQLite with the mock LLM. Live MySQL/SQL Server/PostgreSQL
  latency has not been measured in this environment (no Docker / credentials).
- The circuit breaker and concurrency limit are per process; with N API replicas the
  effective ceiling is N × `pool_max_size`. A Redis-backed distributed limiter is a
  Phase 7 option.
- Cost estimates exist for PostgreSQL and MySQL (EXPLAIN); SQL Server/Oracle use
  metadata row estimates only.
