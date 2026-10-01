# ERP Insight AI — Observability

Logs, metrics, traces and audit events, what they contain (and never contain), and the
runbook for every alert.

## Signals

| Signal | Where | Notes |
|---|---|---|
| Structured logs | stdout, JSON (`LOG_JSON=true`) | request id, tenant id, user id, route; recursive redaction of secrets/tokens/DSNs on every record (`app/core/logging.py`) |
| Metrics | `GET /metrics` on each API process (internal network only, blocked at nginx) | Prometheus registry in `app/core/metrics.py`; labels are low-cardinality (never tenant/user ids or question text) |
| Traces | OTLP/HTTP when `OTEL_ENABLED=true` | one server span per request, W3C `traceparent` propagated; no SQL values, prompts or rows in attributes |
| Correlation | `X-Request-ID` | echoed on every response, included in every log line and error envelope |
| Audit | `audit_events` table (append-only) | one event per question/export/login/admin action; hashes and counts, never rows |
| Health | `/health/live`, `/health/ready` | readiness checks Postgres, Redis, Qdrant with per-check timeouts |

**Never logged or exported as metrics:** database passwords, access/refresh tokens,
one-time download tokens, full result rows, raw prompts, question text in audit
details, SQL literal values (SQL is logged/shown with placeholders only).

## Metrics catalogue

| Area | Metrics |
|---|---|
| HTTP | `erp_http_requests_total{method,route,status}`, `erp_http_request_duration_seconds`, `erp_http_requests_in_progress` |
| Dependencies | `erp_dependency_up{dependency}`, `erp_dependency_check_duration_seconds` |
| Auth / abuse | `erp_auth_events_total{event,outcome}`, `erp_rate_limit_rejections_total{scope}`, `erp_question_guard_refusals_total{rule}` |
| Questions | `erp_analytics_queries_total{mode,status}` (status `cancelled` = query cancellations), `erp_analytics_query_duration_seconds` |
| Pipeline steps | `erp_agent_step_duration_seconds{step,status}` — `COMPILE_DIALECT_SQL` is **query compilation latency**, `EXECUTE_READONLY_QUERY` **execution latency**, `BUILD_QUERY_PLAN`/`CLASSIFY_INTENT`/`GENERATE_EXPLANATION` model steps |
| Model | `erp_llm_request_duration_seconds{provider,outcome}`, `erp_llm_tokens_total{provider,kind}`, `erp_llm_validation_failures_total{provider,schema,stage}`; per answer: `usage` in the response and `llm_calls`/`llm_tokens` in the audit event |
| Retrieval | `erp_retrieval_duration_seconds{cache}`, `erp_embedding_duration_seconds`, `erp_ingestion_*` |
| Caches | `erp_cache_requests_total{cache=query_result\|catalog\|retrieval,result=hit\|miss\|error}` |
| Results | `erp_query_result_rows{connector}` (row-count distribution, cache hits excluded) |
| Connectors | `erp_connector_operations_total{connector,operation,outcome}` (incl. `operation=breaker/queue`), `erp_connector_operation_duration_seconds`, `erp_connector_errors_total{connector,code}` |
| Safety | `erp_sql_safety_rejections_total{reason}` — must stay 0 |
| Exports / UX | `erp_export_jobs_total{event}`, `erp_export_rows_total`, `erp_ws_connections`, `erp_ws_messages_total`, `erp_feedback_*` |

Dashboards (provisioned automatically in Grafana): **ERP Insight AI - API overview**
(traffic, errors, latency, dependencies) and **ERP Insight AI - Analytics and AI**
(outcomes, step latency, model latency/tokens/validation, retrieval, caches, result
rows, connector errors, breaker/queue, safety, exports).

`tests/unit/test_observability_config.py` fails if a dashboard or alert references a
metric the application does not export, or an alert has no runbook section below.

## Alerting

Rules: `infrastructure/prometheus/alerts.yml` (loaded by Prometheus via `rule_files`).
Compose ships Prometheus without Alertmanager: alerts are visible at
`http://prometheus:9090/alerts`. In production, add an Alertmanager (or Grafana
alerting) and route `severity=critical` to paging, `warning` to tickets.

The Celery worker has no metrics endpoint yet; export failures are visible through the
API-side `erp_export_jobs_total{event="failed"}` and the worker's JSON logs.

## Alert runbooks

### ErpInsightApiDown
An API instance is not scraped. Check `docker compose ps api` / `kubectl get pods -l
app=erp-insight-api`, container logs (startup refuses insecure production config and
logs the reason), and `/health/live`. If only one replica is down, the others keep
serving; if all are down, check the database migration job and secrets.

### ErpInsightHigh5xxRatio
Look at `erp_http_requests_total{status=~"5.."}` by route. Common causes: a dependency
down (see next alert), the model server unreachable (`LLM_UNAVAILABLE`, 503), or a bug
(errors carry `request_id`; find the stack trace in the logs by that id).

### ErpInsightDependencyDown
`erp_dependency_up{dependency}` is 0. Postgres: the API cannot authenticate or store
history — restore the database first. Redis: rate limits fail open, caches are skipped
and each call waits for the connect timeout (answers become slow) — restore quickly.
Qdrant: semantic retrieval fails, so questions cannot be planned and return an error
until Qdrant is back; if its volume was lost, rebuild each connection's index
(`POST /connections/{id}/semantic/ingest`).

### ErpInsightSqlSafetyRejection
**Treat as a defect.** A statement compiled from a validated plan failed the
independent SQL validator (the statement was *not* executed). Find the audit event
(`analytics.query`, `error_code=QUERY_NOT_ALLOWED`) and its `plan_hash`; reproduce with
`POST /analytics/explain`; fix the compiler, the semantic layer or the allow-list and
add a regression test (`tests/unit/test_query_engine.py`). Never widen the validator
without a test.

### ErpInsightRefreshTokenReuse
A revoked refresh token was presented; the whole session family was revoked
automatically. Check `auth.refresh_reuse` audit events for the user and IP; contact the
user; consider forcing a password reset.

### ErpInsightGuardRefusalSpike
Many questions were refused by the deterministic guard (write statements or sensitive
fields). Group `analytics.query` audit events by `actor_user_id` with
`refusal_reason` set. A single user probing for bank accounts or `DROP TABLE` is a
security signal; a whole tenant may indicate a missing semantic entity users expect.

### ErpInsightLoginFailureSpike
Credential stuffing or a misconfigured client. nginx and Redis rate limits plus account
lockout already slow attackers; check source IPs in the nginx logs and consider
blocking them upstream.

### ErpInsightLlmErrors
Model calls fail (`outcome=error`). Check the model server (vLLM/Ollama) health, GPU
memory, and `LLM_BASE_URL`. The provider retries transient errors and opens a circuit
breaker; users see `LLM_UNAVAILABLE`.

### ErpInsightLlmInvalidOutput
The model often returns JSON that fails schema validation (each output gets one repair
attempt). Usually a model or prompt change: compare `erp_llm_validation_failures_total`
by `schema`, run `python scripts/run_evals.py` against the model, and roll back the
model (`LLM_MODEL`) if plan validity dropped.

### ErpInsightAnalyticsSlow
p95 answer time is above 5 s. Use the *Pipeline step p95* panel: model steps → model
server capacity; `EXECUTE_READONLY_QUERY` → the ERP database (add a reporting or
materialized view, a read replica, check `QUERY_TOO_EXPENSIVE`); retrieval → Qdrant.
See `docs/performance.md`.

### ErpInsightConnectorCircuitOpen
A customer database failed repeatedly (unreachable or timing out) and the breaker is
rejecting calls for `CONNECTOR_BREAKER_RESET_SECONDS`. Test the connection
(`POST /connections/{id}/test`), check the customer's network/VPN and database status.
It closes by itself after a successful probe.

### ErpInsightConnectorSaturated
Queries wait longer than `CONNECTOR_QUEUE_TIMEOUT_SECONDS` for a pooled connection
(`CONNECTOR_BUSY`). Raise the connection's `pool_max_size` if the database can take it,
add a read replica, or lower concurrency; check for slow queries first.

### ErpInsightConnectorErrors
Repeated `HOST_UNREACHABLE`, `TIMEOUT` or `AUTH_FAILED` for a connector type. For
`AUTH_FAILED`, the stored secret was rotated on the database side — update the
connection's password/secret reference. Connector errors never include driver text in
API responses; the classified code is in the logs.

### ErpInsightExportFailures
Several background exports failed. Check `export.completed` audit events for
`error_code`: `SENSITIVE_DATA_DETECTED` (a view exposes card numbers/IBANs — fix the
semantic layer), `EXPORT_TIMEOUT` (narrow the query or raise `EXPORT_TIMEOUT_SECONDS`),
worker not running (jobs stay `pending`), or the export volume not shared/writable.

## Evaluation as an operational check

Run the evaluation harness after changing the model, prompts or semantic layer:
`python scripts/dev.py eval` (sandbox) or remote mode against staging. See
`docs/evaluation.md`.
