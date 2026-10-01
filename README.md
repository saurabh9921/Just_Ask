# ERP Insight AI

Ask questions about your ERP data in plain English, from a Chrome side panel, and get
governed, **read-only** analytical answers from PostgreSQL, MySQL/MariaDB, SQL Server or
SQLite (Oracle and SAP HANA as opt-in foundations; Snowflake and BigQuery planned).

> *"Show total sales by region for the last six months."* → the local LLM builds a
> structured query plan → the backend validates, authorizes, compiles and executes it
> with a read-only role → you get a table, KPIs, a chart and an explanation.

**Status: Phases 1–7 done** — foundation, sign-in (JWT + rotating refresh tokens),
tenants/RBAC, connection management with encrypted secrets, PostgreSQL and SQLite
connectors, metadata discovery, the extension's sign-in/workspace UI, the governed semantic
layer (drafted from your live database), Qdrant hybrid retrieval, the local-LLM provider layer, and the
governed query engine: structured query plans, SQL compilation, independent SQL safety
validation, read-only execution, result formatting and the `/api/v1/analytics` endpoints
(sync, SSE streaming, explain, follow-up, get, cancel), plus the extension's chat UI with
live WebSocket progress, KPI cards, tables, charts, query-plan viewer, history, saved
questions, feedback (with feedback memory) and CSV/JSON export. Phase 6 adds MySQL and
SQL Server connectors, Oracle/SAP HANA foundations, read replicas, circuit breakers,
per-connection concurrency limits, per-tenant rate limits, a scope-keyed query-result
cache and background "export all rows" jobs with one-time download links
([`docs/performance.md`](docs/performance.md)). Phase 7 adds the evaluation harness
(40 held-out labelled questions, safety gate — [`docs/evaluation.md`](docs/evaluation.md)),
a deterministic write/sensitive-data question guard, per-answer token usage, alert
rules and dashboards with runbooks ([`docs/observability.md`](docs/observability.md)),
Vault secret references and key rotation, Kubernetes manifests and the deployment,
backup/recovery and troubleshooting guide ([`docs/deployment.md`](docs/deployment.md)). See also
[`docs/semantic-layer.md`](docs/semantic-layer.md) and [`docs/local-llm.md`](docs/local-llm.md). See [`docs/architecture.md`](docs/architecture.md),
[`docs/security.md`](docs/security.md), [`docs/api.md`](docs/api.md),
[`docs/connector-development.md`](docs/connector-development.md) and
[`CLAUDE.md`](CLAUDE.md).

## Repository

```
apps/api                 FastAPI backend, Alembic migrations, tests (unit/security/integration)
apps/worker              Celery worker (erp_worker)
apps/browser-extension   Angular 22 Chrome MV3 side-panel extension
infrastructure/          Docker Compose (dev + prod overlay), nginx, Postgres, Redis, Qdrant,
                         Prometheus, Grafana
docs/                    Architecture and guides
scripts/dev.py           Cross-platform task runner (Makefile delegates to it)
```

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.12+ |
| Node.js | `^22.22.3` or `>=24.15` (required by Angular 22) |
| Docker | Docker Desktop / Engine with Compose v2.24+ |
| Chrome | 116+ (side panel API) |

No GPU is needed for local development.

## Quick start: your live ERP database

The product ships **no demo data**. You connect it to a live ERP database with a
**read-only** account, draft the semantic layer from that database's own structure,
review it, and start asking questions.

**Before you start** create a SELECT-only database account on the ERP database (or,
better, a read replica / reporting schema) as described in
[`docs/connector-development.md`](docs/connector-development.md) → *Customer-side
provisioning*. The API refuses accounts that can write.

```bash
# 1. .env with random secrets, Python venv, npm install
#    (picks port 5433 for the platform database if a local PostgreSQL owns 5432)
python scripts/dev.py bootstrap

# 2. In .env choose a model. The default "mock" model only pattern-matches; for real
#    answers on your data use Ollama, e.g.
#      LLM_PROVIDER=ollama  LLM_BASE_URL=http://ollama:11434  LLM_MODEL=qwen2.5:7b-instruct
#      EMBEDDING_PROVIDER=ollama  EMBEDDING_BASE_URL=http://ollama:11434
#      EMBEDDING_MODEL=nomic-embed-text  EMBEDDING_DIMENSIONS=768
#    (start it with --profile ollama and `ollama pull` both models - docs/local-llm.md)

# 3. Start the stack (nginx on http://127.0.0.1:8080)
python scripts/dev.py up
curl http://127.0.0.1:8080/health/ready

# 4. Your tenant and first administrator (password prompted, never an argument)
python scripts/dev.py create-tenant acme "Acme Ltd"
python scripts/dev.py create-user acme admin@acme.com tenant_admin

# 5. Connect the live database: create + test (read-only posture check) + metadata sync
#    (admin and DB passwords are prompted, or read from ERP_ADMIN_PASSWORD / ERP_DB_PASSWORD)
python scripts/dev.py onboard --tenant acme --email admin@acme.com connect \
    --name "ERP production" --type postgresql --host erp-db.internal --port 5432 \
    --database erp --schema reporting --db-user erp_insight_ro --tls verify-full

# 6. Draft the semantic layer from the tables/views you want to expose
python scripts/dev.py onboard --tenant acme --email admin@acme.com draft \
    --name "ERP production" --list                       # see what was discovered
python scripts/dev.py onboard --tenant acme --email admin@acme.com draft \
    --name "ERP production" --objects "sales_*,customers,v_inventory*"
#    -> semantic-layer/entities/*.yaml  REVIEW them: business names, descriptions,
#       synonyms, aggregations; sensitive columns are already excluded.

# 7. Build the index, then ask questions from the extension
python scripts/dev.py onboard --tenant acme --email admin@acme.com ingest --name "ERP production"

# 8. Build and load the extension
python scripts/dev.py ext-build
#    chrome://extensions → Developer mode → Load unpacked → apps/browser-extension/dist
#    Add chrome-extension://<the-extension-id> to CORS_ALLOWED_ORIGINS in .env, then
#    restart the API: docker compose -f infrastructure/docker/docker-compose.yml --env-file .env up -d api
#    Sign in with tenant "acme" and pick "ERP production" as the workspace.
```

Connections from Docker reach your database by its network name or IP; a database on
this same machine is reachable as `host.docker.internal`. Databases without TLS need
`--tls disable` and are refused in production unless `ALLOW_INSECURE_CONNECTION_TLS=true`.
The semantic layer is a folder you own (`semantic-layer/`, see its README); edit it any
time and re-run `onboard ingest`.

Running the API on the host (hot reload) instead of in Docker:

```bash
python scripts/dev.py up-data            # postgres, redis, qdrant only
python scripts/dev.py migrate api        # alembic upgrade head, then uvicorn --reload on :8000
```

### Local LLM options

| `LLM_PROVIDER` | How | When |
|---|---|---|
| `mock` (default) | nothing to run | CPU-only dev and tests |
| `ollama` | `docker compose ... --profile ollama up -d`, then pull a model | local experimentation on CPU |
| `vllm` | `--profile vllm`, set `VLLM_MODEL`, `LLM_MODEL`, `LLM_BASE_URL=http://vllm:8000/v1` | GPU hosts / production |

The model is always configuration (`LLM_MODEL`, `VLLM_MODEL`), never hardcoded.

## Development tasks

| Task | Command |
|---|---|
| Format | `python scripts/dev.py format` |
| Lint | `python scripts/dev.py lint` |
| Type-check (mypy strict, tsc strict) | `python scripts/dev.py typecheck` |
| Unit tests (no Docker) | `python scripts/dev.py test` |
| Security tests | `python scripts/dev.py test-security` |
| Performance budgets | `python scripts/dev.py test-performance` |
| Evaluation harness (safety gate) | `python scripts/dev.py eval` |
| Dependency audit | `python scripts/dev.py audit` |
| Integration tests (needs `up-data`) | `python scripts/dev.py test-integration` |
| Everything | `python scripts/dev.py check` |
| Migrations | `python scripts/dev.py migrate` · review SQL: `python scripts/dev.py migrate-sql` |
| Onboard a live database | `python scripts/dev.py onboard <connect\|draft\|ingest\|status> --help` |
| Production stack | `bootstrap-prod`, `prod-up`, `prod-down`, `prod-logs`, `prod-create-tenant`, `prod-create-user` |

`make <task>` works identically where `make` is available.

## API

Full reference: [`docs/api.md`](docs/api.md).

| Endpoint | Purpose |
|---|---|
| `GET /health/live`, `GET /health/ready`, `GET /metrics` | liveness, readiness (Postgres/Redis/Qdrant), Prometheus (internal only) |
| `POST /api/v1/auth/login · refresh · logout`, `GET /api/v1/auth/me` | sign-in, rotating refresh tokens, sign-out, profile |
| `/api/v1/connections` (CRUD) + `POST …/{id}/test` | governed, read-only data-source connections |
| `POST …/{id}/metadata/sync`, `GET …/{id}/sync-status`, `GET …/{id}/entities[/{name}]` | metadata discovery |
| `/api/v1/admin/users` | tenant user and role administration |
| Analytics, history, feedback | Phases 4–5 |
| `/api/v1/exports` | background exports with one-time download links (Phase 6) |

All errors use one envelope:

```json
{"error": {"code": "QUERY_NOT_ALLOWED", "message": "The requested operation is not permitted.",
           "request_id": "req_…", "details": {}}}
```

## Production (single VM)

```bash
python scripts/dev.py bootstrap-prod     # .env.production with random secrets
#   edit .env.production: EXTENSION_API_ORIGIN (https://your-host), CORS_ALLOWED_ORIGINS
#   (your extension ID), model settings; put fullchain.pem + privkey.pem in TLS_CERT_DIR
python scripts/dev.py prod-up            # validates the file, builds, starts: HTTPS on 443
python scripts/dev.py prod-create-tenant acme "Acme Ltd"
python scripts/dev.py prod-create-user acme admin@acme.com tenant_admin
python scripts/dev.py onboard --api-url https://your-host --tenant acme --email admin@acme.com connect ...
python scripts/dev.py ext-build          # built for EXTENSION_API_ORIGIN - distribute this build
```

The API refuses to start in production with default/weak secrets, wildcard CORS, the
mock model, hashing embeddings, plain-text logs or SQLite connections. Port 80 only
redirects to HTTPS; data services are on an internal network with no published ports;
the extension may call only `EXTENSION_API_ORIGIN`. Run exactly one `scheduler`. For
Kubernetes (kustomize, `infrastructure/kubernetes/`), backups, secrets rotation and
troubleshooting see [`docs/deployment.md`](docs/deployment.md).

## Security at a glance

Read-only by construction (plan-not-SQL, validator, read-only role, server-side read-only
sessions, DB constraint) · tenant isolation · RBAC · no credentials in the browser ·
secret references instead of passwords · append-only audit log · redacted structured
logs · no internals in error responses. Details: `docs/architecture.md` §7.
