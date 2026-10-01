# ERP Insight AI — Security model

Scope: what is enforced today (Phases 1–7) and how. Planned controls are marked *(Phase N)*.
Every control listed as implemented has a regression test in `apps/api/tests/security/`
or `apps/api/tests/unit/`.

## 1. Threat model (summary)

| Asset | Threats | Primary controls |
|---|---|---|
| ERP data | Exfiltration, writes via the assistant, cross-tenant reads | Read-only by construction (§5), tenant scoping (§3), connection grants (§3) |
| ERP credentials | Disclosure via API, logs, DB dump, other tenants | Write-only secrets, AES-GCM with bound AAD, tenant-prefixed env refs (§4) |
| User sessions | Credential stuffing, token theft/replay | Argon2id, rate limits + lockout, rotating refresh with reuse detection (§2) |
| Platform infra | SSRF through "test connection", file read through SQLite | Host policy, TLS policy, SQLite sandbox (§6) |
| Audit trail | Tampering, gaps | Append-only trigger, independent audit transaction (§7) |
| Caches and export files | Cross-tenant/cross-scope reads, stale authorization, bulk exfiltration | Scope-keyed cache, run-time re-authorization, one-time download tokens, expiry, sensitive scan (§11) |

## 2. Authentication

**Login** `POST /api/v1/auth/login {tenant, email, password}`

1. Per-IP (`LOGIN_RATE_LIMIT_PER_IP_PER_MINUTE`) and per-account
   (`LOGIN_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE`) fixed-window limits in Redis. Keys are
   SHA-256 digests (no emails/IPs in Redis) and always have a TTL. Exceeding a limit →
   `429 RATE_LIMITED` with `Retry-After`. If Redis is down the limiter fails *open* and
   logs a warning — the database lockout below still applies.
2. Unknown tenant/user, inactive user, suspended tenant, locked account and wrong
   password all return the identical `401 INVALID_CREDENTIALS`. Unknown accounts still
   run an Argon2 verification against a dummy hash, so timing does not reveal existence.
3. Wrong passwords increment `failed_login_count`; from `LOGIN_LOCKOUT_THRESHOLD` the
   account is locked for `base * 2^(n - threshold)` seconds (capped).
4. Passwords: Argon2id (time 3, 64 MiB, parallelism 4 by default); inputs capped at 128
   chars (DoS guard); hashes transparently upgraded when parameters change. Minimum
   length `PASSWORD_MIN_LENGTH` (12); must not contain the email or its local part.

**Tokens**

| Token | Form | Lifetime | Storage (server) | Storage (extension) |
|---|---|---|---|---|
| Access | JWT HS256/384/512, claims `iss aud sub tid sid jti iat nbf exp typ` | 10 min | none | background worker, `chrome.storage.session` |
| Refresh | 256-bit random, opaque | 12 h absolute | SHA-256 only | background worker, `chrome.storage.session` |

- `sid` binds an access token to its refresh-token *family* (the session).
- **Every request** re-loads the user, tenant and session: deactivated users, suspended
  tenants, logged-out sessions and revoked families are rejected immediately
  (`401 SESSION_REVOKED`), not after the access token expires.
- **Rotation**: each `POST /auth/refresh` revokes the presented token and issues a new
  one in the same family with the *same* absolute expiry.
- **Reuse detection**: presenting an already-revoked refresh token revokes the entire
  family and is audited as `auth.refresh_reuse`. The legitimate client is signed out
  as well — the safe outcome when theft cannot be distinguished from replay.
- `POST /auth/logout` revokes the family and always returns 204 (no token oracle).
- Error codes: `UNAUTHENTICATED`, `TOKEN_EXPIRED` (client should refresh once),
  `SESSION_REVOKED`, `INVALID_CREDENTIALS`, `REFRESH_TOKEN_INVALID`.

**Extension**: the side panel never sees tokens or passwords; the background service
worker performs all API calls, refreshes single-flight 60 s before expiry, retries once
on `TOKEN_EXPIRED`, and clears the session on `SESSION_REVOKED`/`REFRESH_TOKEN_INVALID`.
Session storage is memory-only (sessions end when the browser closes).

## 3. Authorization and tenant isolation

- Tenant ID comes only from the validated token and is re-checked against the user row;
  request bodies never carry tenant IDs. A token re-signed for another tenant fails
  because the user does not exist there.
- RBAC catalog: `app/core/permissions.py` (seeded by migration 0001, drift-tested).
- Connections: holders of `connections.manage` see all live connections in their
  tenant; others see only connections granted to one of their roles
  (`connection_grants`). Foreign or ungranted connections → **404** (no existence oracle).
- Metadata sync requires `connections.manage`; reading entities requires `metadata.read`
  *and* visibility of the connection.
- Admin (`admin.manage`): cannot grant roles above the actor's highest role;
  `platform_admin` only by a platform admin; cannot modify higher-ranked users; cannot
  deactivate or demote oneself. Changes apply on the next request.

## 4. Secrets

- ERP passwords (and CA certificates) are **write-only**. Responses expose only
  `has_secret` and `secret_source` (`encrypted` | `env`).
- `local://<uuid>` → `encrypted_secrets.ciphertext` = `nonce(12) || AES-256-GCM(...)`
  with key = HKDF-SHA256(`ENCRYPTION_KEY`, salt `erp-insight-ai/encrypted-secrets`,
  info `v1`) and **AAD = `tenant_id|secret_id|purpose`**. A ciphertext copied to another
  tenant, row or purpose fails authentication.
- `env://ERP_SECRET_<TENANT_SLUG>_<NAME>` → deployment-injected variables. The tenant
  prefix prevents referencing other tenants' or platform secrets (`JWT_SECRET` etc.).
- `vault://<mount>/<path>#<field>` reads HashiCorp Vault KV v2 (Phase 7) when
  `VAULT_ADDR` is set. The path must start with the tenant's `VAULT_TENANT_PREFIX`
  (default `erp-insight/<tenant>/`, trailing slash, so `acme` cannot reach `acme2`);
  `..`, dot-segments and malformed mounts/fields are rejected (tested). The token comes
  from `VAULT_TOKEN_FILE` (Vault Agent) or `VAULT_TOKEN`, is read per request, never
  logged; production requires `https://`. Vault errors surface as `SECRET_UNAVAILABLE`.
- `aws-sm://`, `gcp-sm://`, `azure-kv://` remain reserved and are rejected.
- Rotating a password stores a new ciphertext and deletes the old one; deleting a
  connection deletes its secrets.
- **Key rotation (Phase 7):** ciphertexts record `key_version`. With
  `ENCRYPTION_KEY_VERSION=n+1` and `ENCRYPTION_KEY_PREVIOUS=<old>` both keys decrypt
  and new secrets use the new key; `python -m app.cli rotate-secrets` re-encrypts
  every old row (idempotent, audited `cli.secrets_rotated`); then the old key is removed.
  Unknown key versions fail closed (`SECRET_UNAVAILABLE`).

## 5. Read-only enforcement for ERP databases

Layers (each independently sufficient to stop a write):

1. API: no `read_only` input field; `db_connections` has `CHECK (read_only = true)`.
2. Adapter: `execute_readonly` accepts only SQLAlchemy `Select` objects built by our code
   (never strings), rejects `FOR UPDATE`, applies the row cap and timeout.
3. Session: PostgreSQL connections set `default_transaction_read_only=on`,
   `statement_timeout`, `lock_timeout`, and each transaction runs
   `SET TRANSACTION READ ONLY`; SQLite opens with `mode=ro` + `PRAGMA query_only=ON`.
   The contract test *bypasses the API and attempts a raw DELETE on the adapter's pool* —
   it must fail on every connector.
4. Role: customers provision a SELECT-only role on reporting views. `test_connection`
   refuses superusers and `BYPASSRLS` roles (`ROLE_TOO_PRIVILEGED`) and warns if the
   role can create objects.
5. Plan validator and SQL validator on compiled SQL (Phase 4, section 10).

## 6. Outbound connection safety (SSRF, files, TLS)

- **Host policy** (`CONNECTION_HOST_DENYLIST`): names and CIDRs, applied to the literal
  host *and* every DNS-resolved address (IPv4-mapped IPv6 included). Defaults cover
  loopback, `0.0.0.0/8`, link-local/cloud metadata, and platform service names; the
  production overlay adds `postgres`. RFC 1918 ranges are allowed (ERP DBs live there).
  *Limitation*: DNS rebinding between check and connect — restrict API egress with
  network policies in production.
- **TLS**: default `verify-full`. `disable`/`prefer` are refused in production unless
  `ALLOW_INSECURE_CONNECTION_TLS=true`.
- **SQLite**: disabled in production (startup refuses `SQLITE_CONNECTIONS_ENABLED=true`);
  files must resolve (after symlinks) inside `SQLITE_DATA_DIR` with a `.db/.sqlite/.sqlite3`
  suffix.
- Connector errors are mapped to stable codes (`AUTH_FAILED`, `HOST_UNREACHABLE`,
  `TLS_ERROR`, ...). Driver messages (which can contain hosts, users, SQL) are never
  returned or logged verbatim — only the exception type is logged.

## 7. Audit

`audit_events` is append-only (PostgreSQL trigger rejects UPDATE/DELETE/TRUNCATE). Events
are written in a separate transaction so denials survive business rollbacks. Current
events: `auth.login` (success/denied + reason), `auth.refresh_reuse`, `auth.logout`,
`connection.create|update|delete|test`, `metadata.sync_requested|sync_completed`,
`semantic.ingestion_requested|ingestion_completed`,
`analytics.query|explain` (one per run, any outcome: success / denied / error, with
status, entity, plan hash, SQL hash, row count, error code, refusal reason, corrections),
`analytics.cancel`, `history.delete`, `feedback.submit` (rating, categories and
whether a comment exists - never the comment),
`admin.user_create|user_update`, `cli.tenant_create|user_create`. Details are redacted
and never contain secrets; unknown-account attempts record an email *fingerprint*.

## 8. RAG and LLM safety (Phase 3)

- **Retrieval scope is mandatory.** `RetrievalScope` (tenant, connection, active version,
  caller permissions) is the only way to query Qdrant; there is no unscoped search. The
  scope is derived from the authenticated context and the connection visibility check.
- **Permission filtering.** Documents carry the permissions their entity requires; a
  document is excluded if it needs any permission the caller lacks.
- **Cache isolation.** Retrieval cache keys embed tenant, connection, semantic version and
  a hash of the caller's permissions (`rag:{tenant}:{connection}:v{n}:{digest}`) with a TTL.
- **Nothing sensitive is indexed.** Only bound entities and their approved objects are
  indexed; columns listed in `forbidden_fields` or matching a `forbid` policy never enter
  the index, so they can never be retrieved or shown to a model. No data values are indexed.
- **Prompt injection.** Text from customer databases (comments) is NFKC-normalised,
  stripped of control characters and code fences, and instruction-like phrases are
  replaced with `[filtered]`; such documents are flagged `untrusted_flagged`. Prompts put
  retrieved text and the question in delimited data blocks whose delimiters cannot be
  forged, under a fixed system prompt that declares them data.
- **Model output is untrusted.** Strict JSON parsing into models that forbid extra keys,
  one repair attempt, then failure. Every name is re-checked against the governed
  vocabulary; unknown entities/fields are dropped and reported, never used.
- **Egress.** Local models by default; external providers need `EXTERNAL_LLM_ENABLED=true`
  and HTTPS. Prompts never contain credentials, detail rows or sensitive fields; the
  Phase 4 explanation prompt carries at most 20 rows of the *aggregated, policy-checked*
  result (sensitive fields cannot be selected by a plan, so they cannot appear there).

## 9. Query safety (Phase 4)

The model's `QueryPlan` is untrusted input. Two independent gates stand between it and
the database, and both are covered by `tests/security/test_phase4_security.py` and
`tests/security/test_sql_injection.py`:

**Plan validator** (`app/query/validator.py`)
- Only entities bound to the connection's metadata snapshot and permitted to the caller
  (`security.required_permissions`); only their declared measures, metrics, dimensions and
  time fields; only approved single-hop joins; measures only from the main entity.
- Forbidden fields (`security.forbidden_fields`, `forbid` policies) → `403
  QUERY_NOT_ALLOWED`; `sensitive` dimensions cannot be projected, filtered or sorted.
  Policy blocks are **not** sent back to the model for correction.
- Filters only on dimensions, values coerced to the column type (bad values rejected),
  operator allow-list, list sizes capped. Sorting only by selected fields.
- Tenant row filters (`security.required_filters`) are added from the authenticated
  tenant - the plan cannot set, remove or override them (a plan filter on `tenant_code` is
  rejected). Required filters with no resolvable value fail closed
  (`ROW_FILTER_UNAVAILABLE`).
- Large facts always get a date window; `limit` is clamped to `MAX_QUERY_ROWS` and the
  connection's `max_rows`.

**Compiler** (`app/query/compiler.py`): SQLAlchemy Core only; identifiers come from the
semantic layer, every value is a bound parameter (`LIKE` patterns auto-escaped), explicit
column projection (no `SELECT *`), `LIMIT` always present. The SQL returned to clients is
rendered with placeholders, never with values.

**SQL validator** (`app/query/sql_validator.py`, sqlglot AST): exactly one `SELECT`; no
comments, DML/DDL/DCL, `INTO`, locking clauses, set operations or procedure calls; tables
and columns must be in an allow-list derived *independently* from the semantic layer; no
system catalogs; function allow-list; tenant row-filter equality must be present in every
`WHERE`; `LIMIT` ≤ cap; join count capped. A rejection of compiled SQL means a
compiler/policy defect: the query fails with `403 QUERY_NOT_ALLOWED` and
`erp_sql_safety_rejections_total` increments.

**Execution**: read-only session, statement timeout, row cap, cancellation, and a cost
gate (`QUERY_MAX_COST`, `QUERY_MAX_SCAN_ROWS` → `422 QUERY_TOO_EXPENSIVE`).

**Agent limits**: allow-listed tools only (no generic database tool), max steps, per-step
and total timeouts, one plan correction, cancellation; every outcome is audited.

**Data minimisation**: `query_history` and audit events store plan/SQL *hashes*, counts and
codes - never result rows or SQL literal values. The full response is cached in Redis for
`QUERY_RESULT_TTL_SECONDS` (default 15 min) under a tenant + user scoped key; set it to 0
to disable. Only the query's owner can read, follow up on or cancel a query.

## 10. Streaming, history and feedback (Phase 5)

**WebSocket** (`/api/v1/analytics/ws`, `app/api/analytics_ws.py`):
- Browsers do not apply CORS to WebSockets, so the handshake checks `Origin` against the
  allowed extension origins (cross-site WebSocket hijacking). No cookies are involved.
- The access token is sent as the first frame, never in the URL (URLs are logged by
  proxies). Every query/cancel re-validates the token and reloads user, tenant and
  session state, so logout, deactivation and revocation apply to open sockets too; a
  socket is bound to one principal.
- Frames are schema-validated (unknown fields rejected), size-limited, and invalid input
  is never echoed. Concurrency per socket is limited; the per-user query rate limit
  applies as for HTTP. Closing the socket cancels its questions.
- nginx proxies only `= /api/v1/analytics/ws` with upgrade headers and a connection limit.

**History and feedback**: every query filters on tenant *and* user; other users' and
other tenants' queries return 404 (tested). History search uses a parameterised `LIKE`
with escaped wildcards. Deletion is soft (the audit trail stays consistent) and removes
the cached answer and feedback memory.

**Feedback memory** (`app/rag/feedback_memory.py`): only question text and the plan
*structure* are indexed - filter values are dropped - with the same tenant, connection,
version and permission payload as curated examples, so retrieval scoping applies. The
plan is re-validated at use time like any model output. Changes invalidate the
connection's retrieval cache. Disable with `FEEDBACK_MEMORY_ENABLED=false`.

**Data-source management in the extension**: only for `connections.manage`; the API
enforces the permission, host policy (SSRF) and read-only posture. Database passwords
typed in the side panel are forwarded once by the background worker, stored encrypted
by the API, never persisted in the browser, never returned (only `hasSecret`) and never
logged; payloads are validated in the background router (hosts cannot carry
connection-string syntax such as `;Trusted_Connection=yes`).

**Extension**: answers live only in the side panel's memory (not extension storage).
Exports are generated locally from returned rows, require `analytics.export` and an
explicit confirmation for multi-row results, and neutralise spreadsheet formulas
(cells starting with `= + - @` or tab/CR are prefixed with `'`). All API bodies and
WebSocket frames are validated by the background worker before reaching the UI.

## 11. Connectors, caching and exports (Phase 6)

**New connectors.** MySQL/MariaDB and SQL Server enforce read-only posture in the
adapter (details: `docs/connector-development.md`): MySQL sessions are
`READ ONLY` with a per-statement time limit and refuse accounts with global write/admin
grants; SQL Server (no session read-only switch) refuses `sysadmin`, `db_owner`,
`db_datawriter`, `db_ddladmin` and database-level write permissions and always rolls back.
The SQL Server ODBC connection string is assembled from validated fields with `{}`
escaping; `;`, `{`, `}` and `=` in host/driver values are rejected (connection-string
injection, tested). Oracle and SAP HANA are *foundations* and cannot be created unless
`ENABLE_FOUNDATION_CONNECTORS=true` (tested). Driver passwords never appear in URLs that
are logged; connection errors are mapped to codes, never raw driver text.

**Read replicas.** `read_replica_host` passes the same SSRF policy as the primary (literal
and DNS-resolved denylist); loopback/metadata addresses and injection characters are
rejected with 422 (tested).

**Result cache** (`app/query/result_cache.py`). Keys contain tenant, connection,
connection-config version, semantic version, a hash of the caller's permissions and
row-filter values, the plan hash and the SQL hash. A hit is served only after
authentication, rate limiting, plan authorization and SQL validation, is audited
(`cache_hit`) and recorded in history. Tests prove that the same question on the same
database never crosses tenants, that a different scope (e.g. `data_admin`) misses, and
that Redis failures fall back to execution. Cached values hold result rows only - no
credentials, SQL literals or tokens.

**Exports** (`app/services/export_service.py`, `app/api/exports.py`):
- Explicit confirmation is required by the API (`confirm: true`), not only the UI.
- Only the owner's own succeeded queries; tenant and user filters on every lookup
  (other users/tenants get 404 for create, status, file, token and delete - tested).
- The worker re-authorizes at run time (active user and tenant, `analytics.export`,
  connection visibility, plan re-validated against the current semantic version, SQL
  validator) - a revoked user produces no file (tested).
- Files are written to `EXPORT_DIR/<tenant_id>/<random>.{csv,json}`; storage keys are
  validated so a tampered key cannot escape the export directory (tested). Partial files
  (`.part`) are deleted on failure. Files expire after `EXPORT_TTL_SECONDS` and are purged
  by the scheduler.
- **Sensitive-data scan before download**: string cells that validate as payment-card
  numbers (Luhn) or IBANs (mod-97) abort the job (`SENSITIVE_DATA_DETECTED`), so such
  values can never be downloaded even if a view exposes them by mistake.
- Downloads: bearer-authenticated `GET /exports/{id}/file`, or a **one-time token**
  (256-bit random, stored only as a SHA-256 hash in Redis, 120 s TTL, deleted on first
  use with `GETDEL`). The token authorizes one file only; a bearer access token is not a
  download token (tested). nginx disables access logging for the token path; the API
  logs route templates only. Responses are `Cache-Control: no-store`.
- CSV cells that spreadsheets evaluate are prefixed with `'` (formula injection).
- Every step is audited: `export.requested`, `export.completed` (rows, bytes or error
  code), `export.downloaded` (`via` token/api), `export.deleted`.

**Extension.** The side panel asks for confirmation, then the background worker creates
the job, polls it, mints a download token and hands the URL to `chrome.downloads` (new
`downloads` permission). The file never passes through extension pages or storage, the
bearer token never appears in a URL, and only same-API paths of the form
`/api/v1/exports/download/<token>` are accepted from the API (tested).

## 12. Evaluation-driven hardening (Phase 7)

**Deterministic question guard** (`app/agents/guards.py`), run before any model call:
- write requests: SQL statement shapes anywhere (`DELETE FROM`, `DROP TABLE`, `UPDATE x
  SET`, `GRANT … TO`, `EXEC sp_…`, …) or an imperative write verb at the start →
  `refused` `WRITE_NOT_ALLOWED`;
- sensitive requests: question terms (words and word pairs, singularised) that *wholly*
  match a `forbid` rule of the sensitive-field policy or name a semantic-layer
  `forbidden_fields` entry → `FORBIDDEN_FIELD` / `SENSITIVE_FIELD`.
The refusal is audited (rule name only, never the question text), counted in
`erp_question_guard_refusals_total{rule}`, and costs no model call (`usage.llm_calls = 0`,
tested). False-positive cases ("Sales in Japan", "orders updated last week", "create a
chart") are tested too. The guard is a first line; the plan validator and SQL validator
remain authoritative.

**Evaluation safety gate:** `scripts/run_evals.py` fails unless SQL safety (an
independent re-check of every SQL shown) and policy compliance (unauthorized, sensitive
and injection cases) are 100% — see `docs/evaluation.md`.

**Deployment hardening:** Kubernetes restricted Pod Security, read-only root
filesystems, no service-account tokens, default-deny NetworkPolicies without a route to
cloud metadata, TLS-only ingress for `/api/` and `/health/` (`docs/deployment.md`).
Dependency audit: `python scripts/dev.py audit` (pip-audit + npm audit; clean on
2026-09-23).

**Still open:** PostgreSQL row-level security as a second tenant barrier (needs a
PostgreSQL environment to verify; see `docs/architecture.md` §14).

## 13. Operational checklist (production)

- [ ] `APP_ENV=production` with unique 32+ char `JWT_SECRET` and `ENCRYPTION_KEY` from a secret manager
- [ ] `CORS_ALLOWED_ORIGINS` = the published extension ID only
- [ ] Redis reachable (rate limits, caches, cross-process cancel) and `erp_sql_safety_rejections_total` alerting on any increase
- [ ] `FORWARDED_ALLOW_IPS` = your proxy/ingress addresses
- [ ] `CONNECTION_HOST_DENYLIST` includes every platform datastore host
- [ ] Egress network policy for the API/worker (only customer ERP ranges)
- [ ] Customers use SELECT-only roles on reporting views; TLS `verify-full`
- [ ] `METADATA_SYNC_MODE=celery` with workers deployed
- [ ] Real embedding model configured (`EMBEDDING_PROVIDER` ≠ `hashing`), Qdrant API key set
- [ ] Audit log shipped to immutable storage; alerts on `auth.refresh_reuse` and lockouts
- [ ] Exactly one `scheduler` (Celery beat) instance, so expired exports are purged
- [ ] `EXPORT_DIR` on an encrypted volume shared only by API and worker; `EXPORT_TTL_SECONDS` as short as users accept
- [ ] `ENABLE_FOUNDATION_CONNECTORS=false` unless you are evaluating Oracle/SAP HANA
- [ ] Alerts on `CONNECTOR_CIRCUIT_OPEN` / `CONNECTOR_BUSY` rates and `erp_export_jobs_total{event="failed"}`
- [ ] `infrastructure/prometheus/alerts.yml` loaded and routed (Alertmanager/Grafana); runbooks in `docs/observability.md`
- [ ] Evaluation harness run against the production model; safety gate 100% (`docs/evaluation.md`)
- [ ] Secrets from Vault (`vault://`) or the platform's secret injection; key rotation procedure rehearsed
- [ ] `python scripts/dev.py audit` clean for the release; images pinned to immutable tags
