# Local LLM, embeddings and reranking

ERP Insight AI runs its models **locally by default**. The model only ever sees the
user's question and governed semantic metadata (entity/field names, descriptions,
synonyms, examples) - never credentials, result rows or sensitive fields - and
every answer is parsed into a strict Pydantic schema and re-validated against the
semantic layer before use.

## Provider matrix

| Setting | Values | Notes |
|---|---|---|
| `LLM_PROVIDER` | `mock` (default), `ollama`, `vllm`, `external` | `mock` is refused in production; `external` also needs `EXTERNAL_LLM_ENABLED=true` and an `https://` URL |
| `LLM_BASE_URL` | e.g. `http://vllm:8000/v1`, `http://ollama:11434` | OpenAI-compatible base for `vllm`/`external`; Ollama root for `ollama` |
| `LLM_MODEL` | any served model name | never hardcoded |
| `EMBEDDING_PROVIDER` | `hashing` (default), `openai_compatible`, `ollama` | `hashing` is refused in production |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | | dimensions must match the model; checked on every response and against existing collections |
| `RERANKER_ENABLED` / `RERANKER_PROVIDER` | `lexical` (default), `http` | `http` = Jina/vLLM-style `POST {base}/rerank` |

## How structured output works

1. Each call names a response model (e.g. `IntentClassification`, `EntityResolution`;
   `QueryPlan` in Phase 4). Models forbid extra keys.
2. vLLM/OpenAI-compatible: `response_format = {"type": "json_schema", "json_schema":
   {"name", "schema", "strict": true}}` with `temperature=0`. Ollama: `format=<schema>`.
3. The reply must be exactly one JSON object (a single Markdown fence is tolerated);
   duplicate keys, `NaN`, extra keys and wrong types are rejected.
4. On rejection the provider makes **one** repair attempt, feeding back only the
   validation errors. A second failure raises `LLM_INVALID_OUTPUT` (502) and is counted
   in `erp_llm_validation_failures_total`.
5. Transient failures (timeouts, connection errors, 408/429/5xx) are retried with
   jittered backoff (`LLM_MAX_RETRIES`); a circuit breaker opens after
   `LLM_CIRCUIT_FAILURE_THRESHOLD` consecutive failures for `LLM_CIRCUIT_RESET_SECONDS`.
6. The validated output is then checked against the governed vocabulary; names
   outside it are dropped and reported (`rejected_names`).

## CPU development (no GPU)

**Option A - mock provider (default).** Deterministic rule-based answers; used by the
test suite. Nothing to run.

**Option B - mock model server over real HTTP.** Exercises the production HTTP code
paths without a GPU:

```bash
python scripts/dev.py mock-llm          # OpenAI/Ollama-compatible server on :8001
# .env
LLM_PROVIDER=vllm
LLM_BASE_URL=http://127.0.0.1:8001/v1
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
EMBEDDING_DIMENSIONS=384
```

**Option C - Ollama.**

```bash
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env --profile ollama up -d ollama
docker compose ... exec ollama ollama pull <chat-model>
docker compose ... exec ollama ollama pull <embedding-model>
# .env
LLM_PROVIDER=ollama
LLM_BASE_URL=http://ollama:11434
LLM_MODEL=<chat-model>
EMBEDDING_PROVIDER=ollama
EMBEDDING_BASE_URL=http://ollama:11434
EMBEDDING_MODEL=<embedding-model>
EMBEDDING_DIMENSIONS=<model output size>
```

Choose a chat model that reliably follows JSON schemas (instruction-tuned models of
7B+ parameters generally do; verify with `python scripts/dev.py eval`, see `docs/evaluation.md`).

## GPU deployment (vLLM)

```bash
# .env
VLLM_MODEL=<hf-repo-or-local-path>        # what vLLM loads
LLM_MODEL=<served-model-name>             # what the API asks for (--served-model-name)
LLM_API_KEY=<random>                      # vLLM --api-key; the API sends it as Bearer
LLM_PROVIDER=vllm
LLM_BASE_URL=http://vllm:8000/v1
docker compose ... --profile vllm up -d vllm
```

Sizing: `VLLM_MAX_MODEL_LEN` (context), `VLLM_GPU_MEMORY_UTILIZATION`, `VLLM_GPU_COUNT`.
Serve the embedding model either from a second vLLM instance (`--task embed`) or a
TEI container, and point `EMBEDDING_BASE_URL` at it with
`EMBEDDING_PROVIDER=openai_compatible`. A cross-encoder reranker can be served the
same way and enabled with `RERANKER_PROVIDER=http`.

## Changing the embedding model

Vector sizes are fixed per collection. The API refuses to reuse a collection whose
size differs from `EMBEDDING_DIMENSIONS` (`VECTOR_STORE_MISCONFIGURED`). To switch
models: set a new `QDRANT_COLLECTION_PREFIX` (e.g. `erp_v2_`), deploy, re-run semantic
ingestion for every connection, then delete the old collections.

## Metrics

`erp_llm_request_duration_seconds{provider,outcome}`, `erp_llm_tokens_total{provider,kind}`,
`erp_llm_validation_failures_total{provider,schema,stage}`,
`erp_embedding_duration_seconds{provider}`, `erp_retrieval_duration_seconds{cache}`,
`erp_cache_requests_total{cache,result}`, `erp_ingestion_documents_total{collection}`,
`erp_ingestion_duration_seconds{outcome}`.
