# ERP Insight AI — API reference (Phases 1–7)

Base path `/api/v1`. JSON only. Authenticate with `Authorization: Bearer <access_token>`.
Interactive OpenAPI docs: `http://127.0.0.1:8000/docs` (development only; disabled in
production and blocked at nginx).

Every response carries `X-Request-ID` (echoed if the caller sent a safe value). Errors:

```json
{"error": {"code": "QUERY_NOT_ALLOWED", "message": "The requested operation is not permitted.",
           "request_id": "req_…", "details": {}}}
```

| HTTP | Common codes |
|---|---|
| 400 | `BAD_REQUEST` |
| 401 | `UNAUTHENTICATED`, `TOKEN_EXPIRED`, `SESSION_REVOKED`, `INVALID_CREDENTIALS`, `REFRESH_TOKEN_INVALID` |
| 403 | `PERMISSION_DENIED` (`details.missing_permissions`), `QUERY_NOT_ALLOWED` (`details.issues` or `details.reason`) |
| 404 | `NOT_FOUND` (also for resources in other tenants / not granted) |
| 400 | `CONFIRMATION_REQUIRED` (exports) |
| 409 | `CONFLICT`, `QUERY_CANCELLED`, `QUERY_IN_PROGRESS`, `IDEMPOTENCY_KEY_REUSED`, `TOO_MANY_EXPORTS`, `EXPORT_NOT_READY`, `QUERY_NOT_EXPORTABLE` |
| 410 | `EXPORT_EXPIRED` |
| 422 | `PLAN_INVALID` (`details.issues[{code,message}]`), `QUERY_TOO_EXPENSIVE`, `VALIDATION_ERROR` (`details.errors[{loc,type,msg}]`, input never echoed), `INVALID_CONNECTION_CONFIG` (`details.reason`), `INVALID_SECRET_REFERENCE`, `CONNECTOR_NOT_SUPPORTED` (also foundation connectors while disabled), `SENSITIVE_DATA_DETECTED` (export job error) |
| 429 | `RATE_LIMITED` (+ `Retry-After`) |
| 504 | `AGENT_LIMIT_EXCEEDED` (`details.reason`: `MAX_STEPS` / `STEP_TIMEOUT` / `TOTAL_TIMEOUT`), `QUERY_TIMEOUT` |
| 503 | `DEPENDENCY_UNAVAILABLE`, `CONNECTOR_UNAVAILABLE`, `CONNECTOR_CIRCUIT_OPEN` (database failing repeatedly; retry shortly), `CONNECTOR_BUSY` (per-connection concurrency limit), `SECRET_UNAVAILABLE` |

## Operations

| Method | Path | Notes |
|---|---|---|
| GET | `/health/live` | process liveness |
| GET | `/health/ready` | `{status, checks:{postgres|redis|qdrant:{status:"up"|"down", latency_ms, required}}}`; 503 when a required check is down |
| GET | `/metrics` | Prometheus; internal network only |

## Authentication

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/auth/login` | `{tenant, email, password}` | `TokenResponse` |
| POST | `/auth/refresh` | `{refresh_token}` | `TokenResponse` (**new** refresh token; the old one is dead) |
| POST | `/auth/logout` | `{refresh_token}` | 204 always |
| GET | `/auth/me` | — | `MeResponse` |

`TokenResponse = {access_token, token_type:"bearer", expires_in, refresh_token, refresh_expires_in, user: MeResponse}`
`MeResponse = {id, email, display_name, tenant:{id, slug, name}, roles[], permissions[]}`

## Connections (`connections.read`; writes need `connections.manage`)

| Method | Path | Notes |
|---|---|---|
| POST | `/connections` | 201 `ConnectionOut` |
| GET | `/connections` | `{items: ConnectionOut[], total}` — only visible connections |
| GET | `/connections/{id}` | 404 if not visible |
| PATCH | `/connections/{id}` | partial update; `password`/`secret_ref` rotate the secret; resets `status` to `untested` |
| DELETE | `/connections/{id}` | 204; soft delete, secrets destroyed, name reusable |
| POST | `/connections/{id}/test` | `{ok, latency_ms, server_version, error_code, message, warnings[]}` |

Create body:

```json
{
  "name": "ERP production", "connector_type": "postgresql",
  "host": "erp-replica.internal", "port": 5432, "database_name": "erp",
  "schema_name": "analytics", "username": "erp_insight_ro",
  "password": "…"            // or "secret_ref": "env://ERP_SECRET_<TENANT>_<NAME>"
  , "tls_mode": "verify-full", "tls_ca_pem": null,
  "read_replica_host": "erp-replica-2.internal", "read_replica_port": 5432,   // optional
  "pool_min_size": 1, "pool_max_size": 5, "query_timeout_seconds": 30, "max_rows": 1000,
  "granted_roles": ["tenant_admin", "data_admin", "analyst", "viewer"]
}
```

There is no `read_only` field — sending one is a 422. `ConnectionOut` never contains the
password or secret reference; it exposes `has_secret` and `secret_source`.

`secret_ref` accepts `env://ERP_SECRET_<TENANT>_<NAME>` and, when Vault is configured,
`vault://<mount>/<VAULT_TENANT_PREFIX><name>#<field>`; `ConnectionOut.secret_source` is
`encrypted` | `env` | `vault` (the reference itself is never returned).

`connector_type`: `postgresql`, `mysql` (also MariaDB), `sqlserver`, `sqlite` (dev only);
`oracle` and `sap_hana` only with `ENABLE_FOUNDATION_CONNECTORS=true`. A read replica
(Phase 6) receives analytical statements; metadata and connection tests use the
primary. The replica host is checked against the same SSRF policy (`422
INVALID_CONNECTION_CONFIG`).

## Metadata

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/connections/{id}/metadata/sync` | `connections.manage` | 202 `SyncStatusOut`; returns the in-flight snapshot if one exists; `Location` → sync-status |
| GET | `/connections/{id}/sync-status` | `metadata.read` | `{connection_id, latest: SyncStatusOut|null, active_version, synced_at}` |
| GET | `/connections/{id}/entities?search=&limit=&offset=` | `metadata.read` | objects of the latest successful snapshot |
| GET | `/connections/{id}/entities/{schema.name or name}` | `metadata.read` | columns (native + canonical type), PK, FKs, row estimate; 409 if an unqualified name is ambiguous |

`SyncStatusOut = {snapshot_id, version, status: pending|running|succeeded|failed, object_count, added_count, changed_count, removed_count, error_code, server_version, created_at, started_at, finished_at}`

## Semantic index (Phase 3)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/connections/{id}/semantic/ingest` | `connections.manage` | 202 `SemanticVersionOut`; needs a successful metadata sync (`409 METADATA_SYNC_REQUIRED`); returns the in-flight build if one exists |
| GET | `/connections/{id}/semantic/status` | `metadata.read` | `{connection_id, latest: SemanticVersionOut|null, active_version}` |
| POST | `/connections/{id}/semantic/search` | `metadata.read` | body `{query (<=1000 chars), collections?, top_k?}` → `{version, hits[{collection, doc_key, title, text, entity_name, score, dense_rank, sparse_rank, untrusted_flagged}]}`; `409 SEMANTIC_INDEX_NOT_READY` before the first build |
| POST | `/connections/{id}/semantic/resolve` | `analytics.query` | body `{question}` → `{version, intent, resolution{entity, measures, dimensions, time_field, confidence, unresolved_terms}, rejected_names, context, llm_provider}` |

`SemanticVersionOut = {version_id, version, status: pending|building|active|superseded|failed, content_hash, embedding_model, embedding_dimensions, document_counts, entity_bindings, error_code, created_at, activated_at, finished_at}`

Model/embedding failures surface as `503 LLM_UNAVAILABLE` / `EMBEDDING_FAILED` / `CIRCUIT_OPEN` or `502 LLM_INVALID_OUTPUT`.

## Analytics (Phase 4, `analytics.query`)

| Method | Path | Notes |
|---|---|---|
| POST | `/analytics/query` | body `{question (1-1000 chars), connection_id}` → `QueryResponse` |
| POST | `/analytics/query/stream` | body `{question, connection_id?, parent_query_id?}` (one of the two ids) → `text/event-stream` |
| POST | `/analytics/explain` | body as `/query`; plans, compiles, validates and costs **without executing** (`status: planned`) |
| POST | `/analytics/follow-up` | body `{question, parent_query_id, connection_id?}`; refines the parent's stored plan |
| GET | `/analytics/query/{id}` | `{query_id, status, question, connection_id, intent, semantic_version, row_count, execution_ms, total_ms, error_code, created_at, completed_at, response: QueryResponse|null, response_expired}` |
| POST | `/analytics/query/{id}/cancel` | `{query_id, status, cancel_requested}`; `cancel_requested: false` when the query already finished |

Only the query's owner can read, follow up on or cancel a query (others get 404).
Starting a run is rate limited per user (`QUERY_RATE_LIMIT_PER_USER_PER_MINUTE`, default 30).

**Idempotency.** Send `Idempotency-Key: <1-128 printable chars>` on the POSTs that start a
run. Within 24 h, repeating the key (same user, question and connection) replays the first
outcome with the same `query_id` and header `Idempotent-Replayed: true`; a different
question → `409 IDEMPOTENCY_KEY_REUSED`; still running → `409 QUERY_IN_PROGRESS`.

**Outcomes.** `200` with `status` = `succeeded` | `planned` | `clarification`
(`clarification_question`) | `refused` (`refusal_reason`: `FORBIDDEN_FIELD`,
`SENSITIVE_FIELD`, `WRITE_NOT_ALLOWED`, `OUT_OF_SCOPE`). Failures use the error envelope
with `details.query_id`: `403 QUERY_NOT_ALLOWED` (policy block, never retried),
`422 PLAN_INVALID` (still invalid after one correction), `404 NOT_FOUND` (connection not
visible), `409 QUERY_CANCELLED`, `409 SEMANTIC_INDEX_NOT_READY`, `504 AGENT_LIMIT_EXCEEDED`,
`502/503` model errors. Every outcome writes one `analytics.query` / `analytics.explain`
audit event and one `query_history` row. Response headers: `X-Query-Id`.

`QueryResponse`:

```jsonc
{
  "query_id": "…", "status": "succeeded", "question": "…", "intent": "aggregation",
  "connection": {"id": "…", "name": "Demo", "connector_type": "postgresql"},
  "semantic_version": 3, "data_freshness": "2026-09-23T06:59:23+00:00",
  "plan": { /* validated QueryPlan */ },
  "description": "total sales (sum of net_amount) by region from SalesOrder for order date 2026-03-16 to 2026-09-15 …",
  "assumptions": ["…"], "source_entities": ["SalesOrder"],
  "applied_filters": {
    "date_window": {"field": "order_date", "start": "2026-03-16", "end_exclusive": "2026-09-16", "label": "…"},
    "previous_window": null,
    "filters": [{"field": "region", "op": "in", "value": ["North", "South"]}]
  },
  "security_scope": {"tenant": "acme", "row_filter_fields": ["tenant_code"], "read_only": true},
  "validation": {"plan_valid": true, "sql_validated": true, "corrections": 0, "confidence": 0.9},
  "sql": "SELECT … WHERE t0.tenant_code = ? AND … LIMIT ? OFFSET ?",  // placeholders, never values
  "previous_sql": null,
  "cost": {"source": "explain", "estimated_cost": 1234.5, "estimated_rows": 4},
  "result": {
    "columns": [{"name": "region", "label": "Region", "kind": "dimension", "type": "string"},
                {"name": "total_sales", "label": "Total sales", "kind": "measure", "type": "decimal", "format": "currency"}],
    "rows": [["South", 1751966.79]], "row_count": 4, "truncated": false,
    "kpis": [{"name": "total_sales", "label": "Total sales", "value": 6008638.33, "scope": "total", …}],
    "chart": {"type": "bar", "x": "region", "y": ["total_sales"], "series": null},
    "redacted_columns": []
  },
  "rows_returned": 4,
  "explanation": {"summary": "…", "highlights": ["…"], "source": "llm"},
  "clarification_question": null, "refusal_reason": null, "error": null,
  "timings": {"total_ms": 412.3, "execution_ms": 18.2, "llm_ms": 250.1},
  "steps": [{"step": "RECEIVE_QUESTION", "status": "completed", "duration_ms": 0.4}, …],
  "replayed": false,
  "cache": {"hit": false, "age_seconds": null},  // Phase 6 result cache
  "usage": {"llm_calls": 3, "prompt_tokens": 2150, "completion_tokens": 310}  // Phase 7
}
```

Comparison results add `<measure>_previous`, `<measure>_change` and `<measure>_change_pct`
columns; `chart.type` is one of `kpi`, `bar`, `grouped_bar`, `line`, `table`.

`cache.hit: true` means the rows came from the query-result cache (key: tenant,
connection, connection version, semantic version, security scope, plan and SQL hash;
TTL `QUERY_CACHE_TTL_SECONDS`); `timings.execution_ms` is then 0 and `age_seconds`
says how old the rows are. Authorization, validation, audit and history still run.
Queries are rate limited per user *and* per tenant (`429 RATE_LIMITED`).

`usage` counts the model calls and tokens spent on this answer (tokens are 0 with the
mock provider). Questions that ask to modify data or name protected fields are refused
*before* any model call (`status: refused`, `refusal_reason` `WRITE_NOT_ALLOWED` /
`FORBIDDEN_FIELD` / `SENSITIVE_FIELD`, `intent: unsupported`, `usage.llm_calls: 0`).

**Streaming** (`/analytics/query/stream`): validation, authorization, rate limit and
idempotency errors are ordinary JSON errors *before* the stream opens. Then:

```
event: accepted
data: {"query_id": "…"}

event: progress
data: {"query_id": "…", "step": "BUILD_QUERY_PLAN", "status": "started", "message": "Planning the query", "elapsed_ms": 812.4, "at": "…"}

event: result            (or: event: error, same QueryResponse shape with "error")
data: { QueryResponse }
```

A `: keep-alive` comment is sent every 15 s. Closing the connection cancels the run.

### WebSocket progress channel (Phase 5)

`GET /api/v1/analytics/ws` (WebSocket; `wss://` behind TLS). One socket carries any
number of questions (`WS_MAX_CONCURRENT_QUERIES` at a time, default 3), identified by a
client-chosen `client_ref` (`[A-Za-z0-9_-]{1,64}`). JSON text frames, at most
`WS_MAX_MESSAGE_BYTES` (16 KiB).

- `Origin`, when sent, must be an allowed extension origin (`CORS_ALLOWED_ORIGINS`),
  otherwise the handshake is rejected (HTTP 403).
- The first frame must be `{"type": "auth", "access_token": "…"}` within
  `WS_AUTH_TIMEOUT_SECONDS` (10 s), else the socket closes with `4401`. Tokens in the URL
  are ignored. Re-send `auth` with a refreshed token at any time (same user only).
- Before every query and cancel the token and the user/tenant/session state are
  re-validated; an expired token yields an `error` frame with `TOKEN_EXPIRED`.

| Client → server | Server → client |
|---|---|
| `{"type":"auth","access_token"}` | `{"type":"ready","user_id","tenant","heartbeat_seconds"}` |
| `{"type":"query","client_ref","question","connection_id"?, "parent_query_id"?, "mode":"execute"\|"plan", "idempotency_key"?}` | `{"type":"accepted","client_ref","query_id"}`, `{"type":"progress","client_ref","query_id","event":ProgressEvent}`, `{"type":"result","client_ref","query_id","response":QueryResponse}` |
| `{"type":"cancel","query_id"}` | `{"type":"cancelled","query_id","status","cancel_requested"}` |
| `{"type":"ping"}` | `{"type":"pong"}`; server heartbeat `{"type":"ping"}` every 20 s |

Errors: `{"type":"error","client_ref"?,"query_id"?,"error":{"code","message","status","details"},"response"?}`
- same codes and statuses as HTTP, plus `INVALID_MESSAGE` (400, input never echoed),
`MESSAGE_TOO_LARGE` (413), `TOO_MANY_QUERIES` (429), `DUPLICATE_CLIENT_REF` (409).
Closing the socket cancels its running questions (they are still audited).

## History (Phase 5, `history.read`, own queries only)

| Method | Path | Notes |
|---|---|---|
| GET | `/history?limit=&offset=&saved=&status=&connection_id=&q=` | newest first → `{items: HistoryItem[], total, limit, offset}`; `q` is a literal substring search (`%`/`_` are not wildcards) |
| GET | `/history/{id}` | `HistoryItem` + `semantic_version`, `execution_ms`, `query_plan`, `response` (cached `QueryResponse` or null), `response_expired` |
| PATCH | `/history/{id}` | `{saved: bool}` → `HistoryItem` (save question) |
| DELETE | `/history/{id}` | 204; hides the query, drops its cached answer and feedback memory, releases its `Idempotency-Key`; audited `history.delete` |

`HistoryItem = {query_id, question, status, intent, connection_id, connection_name, row_count, total_ms, error_code, saved, feedback_rating, created_at, completed_at}`

## Feedback (Phase 5, `feedback.write`)

| Method | Path | Notes |
|---|---|---|
| POST | `/feedback` | `{query_id, rating: -1\|1, categories?: [..≤5], comment?: ≤2000}` → 201 `{id, query_id, user_id, question, rating, categories, comment, created_at, updated_at, remembered}`; one rating per user and query (re-posting updates it); `409 QUERY_NOT_RATEABLE` while running |
| GET | `/feedback?limit=&offset=&rating=&query_id=&scope=mine\|tenant` | own feedback; `scope=tenant` needs `admin.manage` |

Categories: `accurate, helpful, fast, wrong_numbers, wrong_data_source, wrong_period,
wrong_filter, missing_data, confusing, slow, other`. `remembered: true` means the answer
was added to the connection's feedback memory (positive rating on a succeeded query).
Comments are stored only in the database - never logged, audited or sent to a model.

## Administration (`admin.manage`)

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/users?limit=&offset=` | tenant users |
| POST | `/admin/users` | `{email, display_name, password, roles[]}` |
| PATCH | `/admin/users/{id}` | `{display_name?, is_active?, roles?}` |

Bootstrap the first tenant/admin with the CLI (see README).

## Exports (Phase 6, `analytics.export`, own queries only)

| Method | Path | Notes |
|---|---|---|
| POST | `/exports` | `{query_id, format: "csv"\|"json", scope: "result"\|"all_rows", confirm: true}` → **202** `ExportOut`. `confirm` is mandatory (`400 CONFIRMATION_REQUIRED`); only your own succeeded queries (`409 QUERY_NOT_EXPORTABLE`); at most `EXPORT_MAX_ACTIVE_PER_USER` pending/running (`409 TOO_MANY_EXPORTS`) |
| GET | `/exports?limit=` | your exports, newest first → `{items: ExportOut[]}` |
| GET | `/exports/{id}` | `ExportOut` (poll until `status` is terminal) |
| GET | `/exports/{id}/file` | bearer-authenticated download (API clients); `409 EXPORT_NOT_READY`, `410 EXPORT_EXPIRED` |
| POST | `/exports/{id}/download-token` | `{url: "/api/v1/exports/download/<token>", expires_in: 120}` — one-time, short-lived |
| GET | `/exports/download/{token}` | **no bearer** (for browser download managers); the token is deleted on first use; unknown/used/expired → 404 |
| DELETE | `/exports/{id}` | 204; deletes the file |

`ExportOut = {export_id, query_id, format, scope, status: pending|running|succeeded|failed|expired|cancelled, row_count, byte_size, truncated, file_name, error_code, created_at, finished_at, expires_at, downloaded_at}`

- `scope: "result"` re-runs the stored plan with its own limit (the rows of the answer);
  `"all_rows"` removes the interactive cap up to `EXPORT_MAX_ROWS` (`truncated: true` when
  more rows exist).
- The job re-validates everything at run time: user active, `analytics.export` still
  granted, connection still visible, plan valid for the *current* semantic version, SQL
  validator passed. Failures end in `status: failed` with `error_code` (e.g.
  `SENSITIVE_DATA_DETECTED` when a cell looks like a card number or IBAN,
  `EXPORT_TIMEOUT`, `SESSION_REVOKED`, `PERMISSION_DENIED`).
- Files expire `EXPORT_TTL_SECONDS` after completion. Responses carry
  `Cache-Control: no-store`. CSV cells that a spreadsheet would evaluate are prefixed
  with `'`. Audit events: `export.requested`, `export.completed`, `export.downloaded`,
  `export.deleted`.
