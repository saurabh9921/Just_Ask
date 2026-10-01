"""Application configuration.

All configuration comes from environment variables (optionally a ``.env`` file in
development). Secrets are typed as ``SecretStr`` so they never appear in ``repr()``,
logs, or tracebacks.

Production safety: when ``APP_ENV=production`` the settings refuse to load if
development defaults or weak secrets are present. Failing fast at startup is
preferable to running an insecure deployment.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Values shipped in .env.example. They must never reach production.
_KNOWN_DEV_SECRETS = frozenset(
    {
        "replace-in-development",
        "change-me",
        "changeme",
        "secret",
        "local-development-key",
    }
)
MIN_SECRET_LENGTH = 32


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMProviderName(StrEnum):
    VLLM = "vllm"
    OLLAMA = "ollama"
    MOCK = "mock"
    EXTERNAL = "external"


class EmbeddingProviderName(StrEnum):
    HASHING = "hashing"  # deterministic, dependency-free; development/tests only
    OPENAI_COMPATIBLE = "openai_compatible"  # vLLM, TEI, llama.cpp, Ollama /v1
    OLLAMA = "ollama"


class RerankerProviderName(StrEnum):
    LEXICAL = "lexical"
    HTTP = "http"  # Jina/vLLM-style POST /rerank


def _default_semantic_layer_path() -> str:
    # Repository layout: <repo>/apps/api/app/config.py -> <repo>/semantic-layer
    return str(Path(__file__).resolve().parents[3] / "semantic-layer")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # ``FOO=`` in .env / compose means "unset", not an empty string.
        env_ignore_empty=True,
    )

    # ------------------------------------------------------------------ app
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_name: str = "erp-insight-ai"
    app_version: str = "0.1.0"
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside the container network only
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    log_json: bool = True

    # ------------------------------------------------------------- databases
    app_database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://app:app@localhost:5432/erp_insight"
    )
    app_db_pool_size: int = Field(default=10, ge=1, le=200)
    app_db_max_overflow: int = Field(default=10, ge=0, le=200)
    app_db_pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    app_db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    app_db_statement_timeout_ms: int = Field(default=15000, ge=100)

    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None

    # -------------------------------------------------------------- security
    jwt_secret: SecretStr = SecretStr("replace-in-development")
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    jwt_refresh_token_ttl_seconds: int = Field(default=60 * 60 * 12, ge=300, le=60 * 60 * 24 * 30)
    encryption_key: SecretStr = SecretStr("replace-in-development")
    # Key rotation (Phase 7): bump the version, move the old key to *_PREVIOUS, run
    # `python -m app.cli rotate-secrets`, then remove ENCRYPTION_KEY_PREVIOUS.
    encryption_key_version: int = Field(default=1, ge=1, le=1000)
    encryption_key_previous: SecretStr | None = None
    jwt_issuer: str = "erp-insight-ai"
    jwt_audience: str = "erp-insight-extension"

    # HashiCorp Vault (KV v2) for `vault://<mount>/<path>#<field>` secret references.
    # Unset VAULT_ADDR = vault:// references are rejected. Paths are tenant-scoped by
    # VAULT_TENANT_PREFIX ({tenant} = tenant slug), so a tenant cannot read another
    # tenant's (or the platform's) secrets.
    vault_addr: str | None = None
    vault_token: SecretStr | None = None
    vault_token_file: str | None = None  # e.g. written by a Vault Agent sidecar
    vault_namespace: str | None = None
    vault_ca_cert: str | None = None
    vault_tenant_prefix: str = "erp-insight/{tenant}/"
    vault_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # Argon2id cost parameters (OWASP-recommended defaults; tests lower them).
    password_hash_time_cost: int = Field(default=3, ge=1, le=10)
    password_hash_memory_kib: int = Field(default=65536, ge=1024, le=1_048_576)
    password_hash_parallelism: int = Field(default=4, ge=1, le=16)
    password_min_length: int = Field(default=12, ge=8, le=128)

    # Brute-force protection
    login_rate_limit_per_ip_per_minute: int = Field(default=20, ge=1)
    login_rate_limit_per_account_per_minute: int = Field(default=5, ge=1)
    login_lockout_threshold: int = Field(default=5, ge=1, le=100)
    login_lockout_base_seconds: int = Field(default=60, ge=1)
    login_lockout_max_seconds: int = Field(default=3600, ge=1)

    # ERP connection guards
    # Hosts/CIDRs the API must never connect to on behalf of a user (SSRF defence).
    connection_host_denylist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "localhost",
            "127.0.0.0/8",
            "::1/128",
            "0.0.0.0/8",
            "169.254.0.0/16",
            "fe80::/10",
            "metadata.google.internal",
            "redis",
            "qdrant",
            "api",
            "worker",
            "nginx",
            "prometheus",
            "grafana",
            "vllm",
            "ollama",
        ]
    )
    # None = allowed outside production, forbidden in production.
    allow_insecure_connection_tls: bool | None = None
    sqlite_connections_enabled: bool | None = None
    sqlite_data_dir: str = "./data/sqlite"
    connector_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Phase 6: connectors and database performance controls
    enable_foundation_connectors: bool = False  # Oracle, SAP HANA (not live-verified)
    sqlserver_odbc_driver: str = Field(
        default="ODBC Driver 18 for SQL Server", pattern=r"^[A-Za-z0-9 ._()+-]{1,80}$"
    )
    connector_breaker_failures: int = Field(default=5, ge=1, le=100)
    connector_breaker_reset_seconds: float = Field(default=30.0, gt=0, le=3600)
    connector_queue_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    metadata_sync_mode: str = Field(default="inline", pattern="^(inline|celery)$")

    # Chrome extension origins allowed by CORS, e.g. chrome-extension://<32-char-id>
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    trusted_proxy_count: int = Field(default=1, ge=0, le=5)

    # ------------------------------------------------------------ query guard
    max_query_rows: int = Field(default=1000, ge=1, le=100_000)
    max_query_seconds: int = Field(default=30, ge=1, le=600)
    max_agent_steps: int = Field(default=12, ge=1, le=50)
    agent_step_timeout_seconds: float = Field(default=45.0, gt=0, le=600)
    agent_total_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    min_plan_confidence: float = Field(default=0.5, ge=0, le=1)
    query_max_cost: float = Field(default=100_000_000.0, gt=0)
    query_max_scan_rows: int = Field(default=50_000_000, ge=1)
    query_result_ttl_seconds: int = Field(default=900, ge=0, le=86_400)
    idempotency_ttl_seconds: int = Field(default=86_400, ge=60)
    query_rate_limit_per_user_per_minute: int = Field(default=30, ge=1)
    query_cancel_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    query_rate_limit_per_tenant_per_minute: int = Field(default=600, ge=1)
    query_result_cache_enabled: bool = True
    catalog_cache_size: int = Field(default=64, ge=0, le=10_000)
    # Validation-first answers: business analyses ("lead analysis for X"), entity
    # resolution, result/chart validation and the per-request trace log.
    analysis_enabled: bool = True
    # A breakdown field is rejected when more than this share of records is NULL/blank.
    result_max_blank_ratio: float = Field(default=0.5, ge=0, le=1)
    # Status-value grounding queries DISTINCT values; skipped above this row estimate.
    value_grounding_max_rows: int = Field(default=2_000_000, ge=0)
    entity_resolution_max_candidates: int = Field(default=50, ge=2, le=500)
    # Adds questions, entity names, SQL parameters and metric values to the trace log.
    # Development only: these are business data and must not reach production logs.
    analytics_trace_values: bool = False
    # Phase 6: background exports
    export_dir: str = "data/exports"
    export_max_rows: int = Field(default=100_000, ge=1, le=5_000_000)
    export_ttl_seconds: int = Field(default=3600, ge=60, le=7 * 86_400)
    export_batch_size: int = Field(default=2000, ge=10, le=50_000)
    export_max_active_per_user: int = Field(default=2, ge=1, le=20)
    export_download_token_ttl_seconds: int = Field(default=120, ge=10, le=3600)
    export_timeout_seconds: int = Field(default=600, ge=10, le=7200)
    # Phase 5: history, feedback memory and WebSocket progress
    history_page_max: int = Field(default=100, ge=1, le=500)
    feedback_memory_enabled: bool = True
    feedback_memory_max_documents: int = Field(default=500, ge=0, le=10_000)
    ws_auth_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    ws_heartbeat_seconds: float = Field(default=20.0, gt=0, le=300)
    ws_max_concurrent_queries: int = Field(default=3, ge=1, le=20)
    ws_max_message_bytes: int = Field(default=16_384, ge=1024, le=1_048_576)
    query_cache_ttl_seconds: int = Field(default=300, ge=0)
    metadata_cache_ttl_seconds: int = Field(default=900, ge=0)
    retrieval_cache_ttl_seconds: int = Field(default=600, ge=0)

    # ------------------------------------------------------------------- LLM
    llm_provider: LLMProviderName = LLMProviderName.MOCK
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "your-local-model"
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # External (third-party hosted) LLMs are disabled unless explicitly enabled.
    external_llm_enabled: bool = False

    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_output_tokens: int = Field(default=2048, ge=64, le=32768)
    llm_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    llm_circuit_reset_seconds: float = Field(default=30.0, gt=0, le=3600)

    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.HASHING
    embedding_base_url: str | None = None  # defaults to LLM_BASE_URL
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "your-embedding-model"
    embedding_dimensions: int = Field(default=384, ge=8, le=8192)
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    reranker_enabled: bool = True
    reranker_provider: RerankerProviderName = RerankerProviderName.LEXICAL
    reranker_base_url: str | None = None
    reranker_model: str = "your-reranker-model"

    # ------------------------------------------------------------------- RAG
    semantic_layer_path: str = Field(default_factory=_default_semantic_layer_path)
    qdrant_collection_prefix: str = Field(default="erp_", pattern=r"^[a-z0-9_]{0,20}$")
    qdrant_upsert_batch_size: int = Field(default=128, ge=1, le=4096)
    rag_top_k: int = Field(default=8, ge=1, le=50)
    rag_candidate_k: int = Field(default=40, ge=1, le=500)
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    rag_max_query_chars: int = Field(default=1000, ge=10, le=10_000)

    # --------------------------------------------------------- observability
    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "erp-insight-api"

    # ----------------------------------------------------------- health/ready
    readiness_required_checks: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["postgres", "redis", "qdrant"]
    )
    readiness_check_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # ----------------------------------------------------------------- worker
    celery_broker_url: SecretStr | None = None
    celery_result_backend: SecretStr | None = None

    # ------------------------------------------------------------ validators
    @field_validator(
        "cors_allowed_origins",
        "readiness_required_checks",
        "connection_host_denylist",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    @field_validator("jwt_algorithm")
    @classmethod
    def _restrict_jwt_algorithm(cls, value: str) -> str:
        # 'none' and asymmetric algorithms without key management are rejected.
        allowed = {"HS256", "HS384", "HS512"}
        if value not in allowed:
            raise ValueError(f"jwt_algorithm must be one of {sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Self:
        if self.app_env not in {AppEnv.PRODUCTION, AppEnv.STAGING}:
            return self

        problems: list[str] = []
        for name in ("jwt_secret", "encryption_key"):
            secret: SecretStr = getattr(self, name)
            raw = secret.get_secret_value()
            if raw.lower() in _KNOWN_DEV_SECRETS or len(raw) < MIN_SECRET_LENGTH:
                problems.append(
                    f"{name.upper()} must be a unique random value of at least "
                    f"{MIN_SECRET_LENGTH} characters"
                )
        if any(origin.strip() == "*" for origin in self.cors_allowed_origins):
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*'")
        if self.llm_provider is LLMProviderName.MOCK:
            problems.append("LLM_PROVIDER=mock is not allowed outside development/test")
        if not self.log_json:
            problems.append("LOG_JSON must be true so logs are machine-parseable")
        if self.embedding_provider is EmbeddingProviderName.HASHING:
            problems.append("EMBEDDING_PROVIDER=hashing is for development/test only")
        if self.sqlite_connections_enabled:
            problems.append("SQLITE_CONNECTIONS_ENABLED must be false (development/testing only)")
        if self.vault_addr and not self.vault_addr.lower().startswith("https://"):
            problems.append("VAULT_ADDR must use https://")
        if self.encryption_key_previous is not None and (
            self.encryption_key_previous.get_secret_value()
            == self.encryption_key.get_secret_value()
        ):
            problems.append("ENCRYPTION_KEY_PREVIOUS must differ from ENCRYPTION_KEY")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _external_llm_guard(self) -> Self:
        if self.llm_provider is LLMProviderName.EXTERNAL and not self.external_llm_enabled:
            raise ValueError(
                "LLM_PROVIDER=external is disabled by default; set EXTERNAL_LLM_ENABLED=true "
                "after a data-processing review"
            )
        return self

    @model_validator(mode="after")
    def _vault_guard(self) -> Self:
        if "{tenant}" not in self.vault_tenant_prefix:
            raise ValueError("VAULT_TENANT_PREFIX must contain {tenant}")
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.app_env in {AppEnv.PRODUCTION, AppEnv.STAGING}

    @property
    def insecure_connection_tls_allowed(self) -> bool:
        if self.allow_insecure_connection_tls is None:
            return not self.is_production
        return self.allow_insecure_connection_tls

    @property
    def sqlite_allowed(self) -> bool:
        if self.sqlite_connections_enabled is None:
            return not self.is_production
        return self.sqlite_connections_enabled

    @property
    def broker_url(self) -> str:
        source = self.celery_broker_url or self.redis_url
        return source.get_secret_value()

    @property
    def result_backend_url(self) -> str:
        source = self.celery_result_backend or self.redis_url
        return source.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
