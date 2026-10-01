# ERP Insight AI — Deployment and operations

| Mode | Files | Use |
|---|---|---|
| Local development | `infrastructure/docker/docker-compose.yml` | CPU only, mock or Ollama model — see the README quick start |
| Single VM | base + `docker-compose.prod.yml` | small installations; TLS at nginx |
| Kubernetes | `infrastructure/kubernetes/` (kustomize) | horizontally scaled production |

Related: `docs/security.md` (production checklist), `docs/local-llm.md` (models),
`docs/observability.md` (dashboards, alerts, runbooks), `docs/performance.md`.

## 1. Browser extension installation

**Development:** `python scripts/dev.py ext-build`, then `chrome://extensions` →
Developer mode → *Load unpacked* → `apps/browser-extension/dist`. Copy the extension ID
into `CORS_ALLOWED_ORIGINS=chrome-extension://<id>` and restart the API. Open the side
panel, set the API URL in *Settings* (default `http://localhost:8080`), sign in.

**Production:** publish to the Chrome Web Store (unlisted) or self-host the CRX so the
extension ID is fixed, then force-install it with enterprise policy (Google Admin /
MDM). `infrastructure/kubernetes/extension-config.yaml` contains an `ExtensionSettings`
policy example that also restricts the extension's host access to your API origin.
Before publishing, change `host_permissions` in `apps/browser-extension/manifest.json`
from `http://localhost:8080/*` to your API origin (`https://erp-insight.example.com/*`)
and rebuild. The extension stores only settings and short-lived tokens (session
storage); answers stay in memory.

## 2. Environment configuration

`.env.example` documents every variable; the most important groups:

| Group | Variables |
|---|---|
| Runtime | `APP_ENV` (`production` enables the safety guard), `LOG_LEVEL`, `LOG_JSON` |
| Datastores | `APP_DATABASE_URL`, `REDIS_URL`, `QDRANT_URL`, `QDRANT_API_KEY` |
| Security | `JWT_SECRET`, `ENCRYPTION_KEY` (+ `ENCRYPTION_KEY_VERSION`, `ENCRYPTION_KEY_PREVIOUS` during rotation), `CORS_ALLOWED_ORIGINS`, `CONNECTION_HOST_DENYLIST`, `FORWARDED_ALLOW_IPS` |
| Secret managers | `VAULT_ADDR`, `VAULT_TOKEN_FILE`/`VAULT_TOKEN`, `VAULT_NAMESPACE`, `VAULT_CA_CERT`, `VAULT_TENANT_PREFIX` |
| Model | `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `EXTERNAL_LLM_ENABLED`, `EMBEDDING_*`, `RERANKER_*` |
| Query limits | `MAX_QUERY_ROWS`, `MAX_QUERY_SECONDS`, `MAX_AGENT_STEPS`, rate limits, `QUERY_CACHE_TTL_SECONDS` |
| Connectors | `ENABLE_FOUNDATION_CONNECTORS`, `SQLSERVER_ODBC_DRIVER`, breaker/queue settings |
| Exports | `EXPORT_DIR`, `EXPORT_MAX_ROWS`, `EXPORT_TTL_SECONDS`, `EXPORT_MAX_ACTIVE_PER_USER` |

With `APP_ENV=production` the API refuses to start with default or short secrets,
wildcard CORS, the mock model, hashing embeddings, plain-text logs, SQLite connections,
a non-HTTPS `VAULT_ADDR`, or `ENCRYPTION_KEY_PREVIOUS` equal to `ENCRYPTION_KEY`.

## 3. Single VM (Docker Compose)

```bash
python scripts/dev.py bootstrap-prod     # .env.production from .env.production.example, random secrets
```

Edit `.env.production`:

| Setting | Value |
|---|---|
| `EXTENSION_API_ORIGIN` | `https://<this server's DNS name>` |
| `CORS_ALLOWED_ORIGINS` | `chrome-extension://<your published extension ID>` |
| `TLS_CERT_DIR` | directory with `fullchain.pem` and `privkey.pem` for that name |
| model | `LLM_*` / `EMBEDDING_*` (defaults: Ollama in compose, `--profile ollama` is added automatically) |
| `ALLOW_INSECURE_CONNECTION_TLS` | leave unset unless your ERP database has no TLS (risk acceptance) |

```bash
python scripts/dev.py prod-up            # refuses to start with REPLACE_* values or missing certs;
                                         # builds images locally (or pulls REGISTRY/IMAGE_TAG), starts
python scripts/dev.py prod-create-tenant acme "Acme Ltd"
python scripts/dev.py prod-create-user acme admin@acme.com tenant_admin   # password prompted
docker compose -f infrastructure/docker/docker-compose.yml -f infrastructure/docker/docker-compose.prod.yml \
  --env-file .env.production --profile ollama exec ollama ollama pull qwen2.5:7b-instruct   # + embedding model
```

Then onboard the live database (section 3a) and build the extension for this origin
(`python scripts/dev.py ext-build` reads `EXTENSION_API_ORIGIN` from `.env`; for a
production build set it there or run `npm run build -- --api-origin=https://<host>` in
`apps/browser-extension`). `prod-logs` follows logs, `prod-down` stops (volumes stay).

What production changes compared with development: `APP_ENV=production` (the API refuses
weak secrets, wildcard CORS, the mock model, hashing embeddings, SQLite and non-JSON
logs); port 80 only redirects to HTTPS (`infrastructure/nginx/prod-http`); data services
run on an internal network without published ports; resource limits and log rotation;
the semantic layer is mounted read-only from `SEMANTIC_LAYER_DIR`; `migrate` runs Alembic
before the API; exactly one `scheduler`.

Redeploying or scaling the `api` service needs no nginx restart: nginx re-resolves the
`api` name through Docker's DNS every 10 s (`resolve` on the upstream), so it follows
recreated containers to their new addresses.

## 3a. Onboarding a live ERP database

**From the extension (tenant/data admins):** *Data sources* → **Add data source** (type,
host, database, read-only user, password, TLS). Every successful connection test then
prepares the source automatically - *Connection tested → Syncing tables → Tables synced →
Building index → Index built successfully* - skipping steps that are already current
(a new source, or changed host/port/database/schema/user, always re-syncs). The index
build needs the semantic layer (step 3 below); until it exists the card says so and shows
the draft commands. **Sync tables** / **Build index** stay on each card to force a re-run;
edit and delete are there too. The command-line steps below do the same and are needed
for drafting the semantic layer (step 3).

1. **Database side:** create a SELECT-only account limited to reporting views or a read
   replica (`docs/connector-development.md` → *Customer-side provisioning*). The
   connection test refuses accounts that can write.
2. **Connect** (creates the connection, tests it, syncs metadata; the database password
   is prompted or read from `ERP_DB_PASSWORD`, or pass `--secret-ref vault://...`):
   ```bash
   python scripts/dev.py onboard --api-url https://<host> --tenant acme --email admin@acme.com \
       connect --name "ERP production" --type sqlserver --host erp-sql.internal \
       --database ERP --schema reporting --db-user erp_insight_ro --tls verify-full
   ```
3. **Draft** the semantic layer from selected tables/views
   (`draft --list` shows what was discovered, `--objects "reporting.v_*"` selects):
   entities land in `semantic-layer/entities/`, foreign-key joins in `semantic-layer/joins/`.
   Sensitive columns are excluded automatically.
4. **Review** the drafts (business names, descriptions, synonyms, aggregations, row
   filters for shared databases) and add examples for common questions
   (`docs/semantic-layer.md`). Copy the folder to the server's `SEMANTIC_LAYER_DIR` if you
   drafted elsewhere.
5. **Ingest**: `onboard ... ingest --name "ERP production"`; users can now ask questions.
6. **Evaluate**: copy `evals/questions.example.yaml` to `evals/questions.yaml`, add your
   own labelled questions and run `scripts/run_evals.py --mode remote --dataset
   evals/questions.yaml ...` (`docs/evaluation.md`). The safety cases must pass.

Re-run `connect` (it reuses the connection and re-syncs) after schema changes, and
`ingest` after every semantic-layer edit.

## 4. Kubernetes

Prerequisites: an ingress-nginx controller, a TLS certificate (`erp-insight-tls`, e.g.
from cert-manager), a **ReadWriteMany** storage class for exports, a managed PostgreSQL
(recommended; the manifests do not include Postgres), optionally a managed Redis, and a
GPU node pool for vLLM.

```bash
# 1. Secrets (never commit them): one env file from your secret manager
kubectl create namespace erp-insight   # or let kustomize create it
kubectl -n erp-insight create secret generic erp-insight-secrets --from-env-file=secrets.env
#   secrets.env keys: APP_DATABASE_URL, REDIS_URL, REDIS_PASSWORD, JWT_SECRET,
#   ENCRYPTION_KEY, QDRANT_API_KEY, LLM_API_KEY (+ VAULT_TOKEN if used)

# 2. Replace the REPLACE_* placeholders: extension ID (extension-config.yaml), host
#    (ingress.yaml), network ranges (networkpolicies.yaml), images/tags (kustomization.yaml)

# 3. Render and review, then apply
kubectl kustomize infrastructure/kubernetes | less
kubectl apply -k infrastructure/kubernetes
kubectl -n erp-insight wait --for=condition=complete job/migrate --timeout=10m
kubectl -n erp-insight rollout status deploy/api deploy/worker deploy/scheduler

# 4. Optional GPU model server
kubectl -n erp-insight apply -f infrastructure/kubernetes/optional/vllm.yaml
```

**Semantic layer on Kubernetes:** the API and worker images copy `semantic-layer/` at
build time. After onboarding and reviewing drafts (section 3a, run against the cluster's
API URL), rebuild and roll out the images, then run `onboard ... ingest`.

What the manifests enforce (and `tests/unit/test_kubernetes_manifests.py` checks):
restricted Pod Security Standard on the namespace; non-root, read-only root filesystem,
all capabilities dropped and RuntimeDefault seccomp for every container; no service
account tokens mounted; resource limits and probes everywhere; secrets only via
`secretKeyRef`/`envFrom` (no Secret objects in git); default-deny NetworkPolicies with
explicit flows (ingress → API; app → Redis/Qdrant/model/Postgres/DNS; app → customer ERP
ranges) and no route to cloud metadata; ingress exposes only `/api/` and `/health/`
over TLS; a single-replica `Recreate` scheduler; HPA (CPU) and PodDisruptionBudgets for
API and worker.

**Upgrades:** build and push new images with an immutable tag → update `newTag` in
`kustomization.yaml` → delete the finished `migrate` Job → `kubectl apply -k` → wait for
the Job → the API rolls out (maxUnavailable 0). Write migrations to be backwards
compatible for one release (expand, then contract) so the previous API version keeps
working during the rollout. **Rollback:**
set the previous tag and re-apply; run `alembic downgrade -1` only if the release notes
say the migration is not backwards compatible.

**Prometheus:** API pods carry `prometheus.io/scrape` annotations; if you use the
Prometheus Operator, create a PodMonitor for `app.kubernetes.io/name=erp-insight-api` and
load `infrastructure/prometheus/alerts.yml` as a PrometheusRule. The NetworkPolicy
allows scrapes from a namespace named `monitoring`.

## 5. GPU deployment (vLLM)

See `docs/local-llm.md` → *GPU deployment*. Summary: set `LLM_PROVIDER=vllm`,
`LLM_BASE_URL=http://vllm:8000/v1`, `LLM_MODEL=<served name>`; the model is
configuration only. Pre-download weights into the `erp-insight-models` volume (the
network policies deny internet egress). Size `--max-model-len` and
`--gpu-memory-utilization` to the GPU. Validate every new model with the evaluation
harness before switching (`docs/evaluation.md`).

## 6. Secrets

| Reference | Use |
|---|---|
| `local://<uuid>` | password typed into the connection form; AES-256-GCM in the database, key from `ENCRYPTION_KEY` |
| `env://ERP_SECRET_<TENANT>_<NAME>` | injected variables (Kubernetes Secret, CSI driver) |
| `vault://<mount>/<prefix><name>#<field>` | HashiCorp Vault KV v2; `<prefix>` = `VAULT_TENANT_PREFIX` for the tenant (default `erp-insight/<tenant>/`), e.g. `vault://secret/erp-insight/acme/erp-prod#password` |

Give the platform's Vault token a read-only policy on `secret/data/erp-insight/*`
only, and prefer a Vault Agent sidecar writing `VAULT_TOKEN_FILE` over a static token.

**Rotating `ENCRYPTION_KEY`:**

1. Deploy with `ENCRYPTION_KEY=<new>`, `ENCRYPTION_KEY_VERSION=<old version + 1>`,
   `ENCRYPTION_KEY_PREVIOUS=<old>` (both keys can decrypt; new secrets use the new key).
2. Run `python -m app.cli rotate-secrets` (Kubernetes: `kubectl -n erp-insight exec
   deploy/api -- python -m app.cli rotate-secrets`). It re-encrypts every `local://`
   secret, is idempotent and audited (`cli.secrets_rotated`).
3. Remove `ENCRYPTION_KEY_PREVIOUS` and redeploy.

Rotating `JWT_SECRET` signs everyone out (access tokens become invalid; refresh tokens
are opaque and survive, so clients silently obtain new access tokens).

## 7. Backup and recovery

| Data | Importance | Backup | Recovery |
|---|---|---|---|
| Application PostgreSQL (tenants, users, roles, connections, encrypted secrets, semantic versions, history, feedback, **audit log**, export jobs) | critical | managed PITR, or nightly `pg_dump -Fc` + WAL archiving; keep ≥ 30 days; copy off-site | restore the dump / PITR, then `alembic upgrade head` |
| `ENCRYPTION_KEY` (and `JWT_SECRET`) | critical — without it `local://` secrets are unrecoverable | secret manager with versioning; escrow separately from DB backups | restore from the secret manager |
| Qdrant (semantic index, feedback memory) | rebuildable | collection snapshots (`POST /collections/{name}/snapshots`) or volume snapshots, daily | restore snapshots; or rebuild: `POST /connections/{id}/semantic/ingest` for each connection (ingestion also rebuilds the feedback memory from the `feedback` table) |
| Redis (rate limits, caches, broker, download tokens) | ephemeral | none needed | starts empty; in-flight Celery jobs are re-queued (`acks_late`) |
| Export files | ephemeral (TTL 1 h) | none | users re-run the export |
| Semantic layer YAML | source code | git | redeploy |

Restore order: secrets → PostgreSQL → migrations → API/worker → Qdrant (snapshot or
re-ingest) → verify with `/health/ready`, a test question and `python scripts/run_evals.py
--mode remote`. Test a full restore at least quarterly; audit events survive restores
because they live in the same database (ship them to immutable storage too).

## 8. Query troubleshooting

Start from the answer's **query id** (`X-Query-Id` header, history, or `details.query_id`)
or **request id** (`X-Request-ID`): the `analytics.query` audit event holds the plan
hash, SQL hash, row count, error code, cache hit and token usage; the logs hold the
step timings. `POST /api/v1/analytics/explain` shows the plan and SQL without executing.

| Symptom / code | Likely cause | What to do |
|---|---|---|
| `clarification` for clear questions | missing synonym/term; entity not bound to the catalog | add synonyms or glossary terms; check `GET /connections/{id}/semantic/status` for unbound entities; re-ingest |
| `refused` `FORBIDDEN_FIELD` / `SENSITIVE_FIELD` | the question names protected data (deterministic guard or validator) | expected; if a legitimate business term collides, rename the semantic field or adjust the sensitive-field policy (security review) |
| `refused` `WRITE_NOT_ALLOWED` | write verb / SQL statement in the question | expected — the platform is read-only |
| `422 PLAN_INVALID` | the model used unknown fields twice | improve examples for that question type; check model quality with the eval harness |
| `403 QUERY_NOT_ALLOWED` | plan referenced a forbidden field or unpermitted entity, or SQL failed the safety validator | the former is policy; the latter is a defect (see `ErpInsightSqlSafetyRejection` runbook) |
| `409 SEMANTIC_INDEX_NOT_READY` | no active semantic index for the connection | run metadata sync, then semantic ingest |
| `422 QUERY_TOO_EXPENSIVE` | EXPLAIN cost above the threshold | narrow the date range; add a reporting/materialized view (`docs/performance.md`) |
| `504 QUERY_TIMEOUT` | database slower than `MAX_QUERY_SECONDS` | read replica, indexes/partitions on the time field, materialized views |
| `503 CONNECTOR_CIRCUIT_OPEN` / `CONNECTOR_BUSY` | database failing / pool exhausted | see the connector runbooks in `docs/observability.md` |
| `503 LLM_UNAVAILABLE`, `502 LLM_INVALID_OUTPUT` | model server down or returning invalid JSON | check vLLM/Ollama; evaluate the model |
| wrong numbers | wrong measure/filter/period chosen, or a view definition issue | compare the plan in the answer (*Query plan* tab) with intent; fix synonyms/examples; check the view; add an eval case |
| cached answer looks stale | result cache (`cache.hit`, `age_seconds`) | wait for `QUERY_CACHE_TTL_SECONDS` or disable the cache for real-time use |
| `429 RATE_LIMITED` | per-user or per-tenant limit | raise `QUERY_RATE_LIMIT_*` if the database can take it |
