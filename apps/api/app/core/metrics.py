"""Prometheus metrics.

Metrics live in a dedicated registry (not the global default) so tests can create
many app instances without duplicate-registration errors, and so only metrics we
define are exported.

Label cardinality rule: labels use *route templates* (``/api/v1/history/{query_id}``),
never raw paths, tenant IDs, user IDs or query text.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest

REGISTRY: Final = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS_TOTAL: Final = Counter(
    "erp_http_requests_total",
    "HTTP requests processed",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION: Final = Histogram(
    "erp_http_request_duration_seconds",
    "HTTP request latency",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)
HTTP_REQUESTS_IN_PROGRESS: Final = Gauge(
    "erp_http_requests_in_progress",
    "HTTP requests currently being processed",
    labelnames=("method",),
    registry=REGISTRY,
)
DEPENDENCY_CHECK_DURATION: Final = Histogram(
    "erp_dependency_check_duration_seconds",
    "Readiness dependency check latency",
    labelnames=("dependency", "status"),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0),
    registry=REGISTRY,
)
DEPENDENCY_UP: Final = Gauge(
    "erp_dependency_up",
    "1 if the dependency passed its last readiness check, else 0",
    labelnames=("dependency",),
    registry=REGISTRY,
)
APP_INFO: Final = Gauge(
    "erp_app_info",
    "Static application build information",
    labelnames=("version", "environment"),
    registry=REGISTRY,
)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# ------------------------------------------------------------------ Phase 2
RATE_LIMIT_REJECTIONS: Final = Counter(
    "erp_rate_limit_rejections_total",
    "Requests rejected by rate limiting",
    labelnames=("scope",),
    registry=REGISTRY,
)
AUTH_EVENTS: Final = Counter(
    "erp_auth_events_total",
    "Authentication events",
    labelnames=("event", "outcome"),
    registry=REGISTRY,
)
CONNECTOR_OPERATIONS: Final = Counter(
    "erp_connector_operations_total",
    "Connector operations by connector type and outcome",
    labelnames=("connector", "operation", "outcome"),
    registry=REGISTRY,
)
CONNECTOR_OPERATION_DURATION: Final = Histogram(
    "erp_connector_operation_duration_seconds",
    "Connector operation latency",
    labelnames=("connector", "operation"),
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)
METADATA_SYNC_DURATION: Final = Histogram(
    "erp_metadata_sync_duration_seconds",
    "Metadata synchronisation duration",
    labelnames=("connector", "outcome"),
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0),
    registry=REGISTRY,
)

# ------------------------------------------------------------------ Phase 3
LLM_REQUEST_DURATION: Final = Histogram(
    "erp_llm_request_duration_seconds",
    "LLM request latency (including retries)",
    labelnames=("provider", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0),
    registry=REGISTRY,
)
LLM_TOKENS: Final = Counter(
    "erp_llm_tokens_total",
    "Tokens reported by the model server",
    labelnames=("provider", "kind"),
    registry=REGISTRY,
)
LLM_VALIDATION_FAILURES: Final = Counter(
    "erp_llm_validation_failures_total",
    "Model outputs rejected by schema validation",
    labelnames=("provider", "schema", "stage"),
    registry=REGISTRY,
)
EMBEDDING_DURATION: Final = Histogram(
    "erp_embedding_duration_seconds",
    "Embedding batch latency",
    labelnames=("provider",),
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0),
    registry=REGISTRY,
)
RETRIEVAL_DURATION: Final = Histogram(
    "erp_retrieval_duration_seconds",
    "Hybrid retrieval latency",
    labelnames=("cache",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)
CACHE_REQUESTS: Final = Counter(
    "erp_cache_requests_total",
    "Cache lookups by cache and result",
    labelnames=("cache", "result"),
    registry=REGISTRY,
)
INGESTION_DOCUMENTS: Final = Counter(
    "erp_ingestion_documents_total",
    "Documents embedded and upserted into the vector store",
    labelnames=("collection",),
    registry=REGISTRY,
)
INGESTION_DURATION: Final = Histogram(
    "erp_ingestion_duration_seconds",
    "Semantic ingestion run duration",
    labelnames=("outcome",),
    buckets=(0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0),
    registry=REGISTRY,
)

# ------------------------------------------------------------------ Phase 4
ANALYTICS_QUERIES: Final = Counter(
    "erp_analytics_queries_total",
    "Analytics agent runs by mode and terminal status",
    labelnames=("mode", "status"),
    registry=REGISTRY,
)
ANALYTICS_QUERY_DURATION: Final = Histogram(
    "erp_analytics_query_duration_seconds",
    "End-to-end analytics agent run latency",
    labelnames=("mode", "status"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0),
    registry=REGISTRY,
)
AGENT_STEP_DURATION: Final = Histogram(
    "erp_agent_step_duration_seconds",
    "Analytics agent state duration",
    labelnames=("step", "status"),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 45.0),
    registry=REGISTRY,
)
SQL_SAFETY_REJECTIONS: Final = Counter(
    "erp_sql_safety_rejections_total",
    "Compiled statements rejected by the SQL safety validator",
    labelnames=("reason",),
    registry=REGISTRY,
)

# ------------------------------------------------------------------ Phase 5
FEEDBACK_SUBMISSIONS: Final = Counter(
    "erp_feedback_submissions_total",
    "Answer ratings by value",
    labelnames=("rating",),
    registry=REGISTRY,
)
FEEDBACK_MEMORY_UPDATES: Final = Counter(
    "erp_feedback_memory_updates_total",
    "Feedback-memory documents added to or removed from retrieval",
    labelnames=("action", "outcome"),
    registry=REGISTRY,
)
WS_CONNECTIONS: Final = Gauge(
    "erp_ws_connections",
    "Open analytics WebSocket connections",
    registry=REGISTRY,
)
WS_MESSAGES: Final = Counter(
    "erp_ws_messages_total",
    "Analytics WebSocket client messages by type and outcome",
    labelnames=("type", "outcome"),
    registry=REGISTRY,
)

# ------------------------------------------------------------------ Phase 6
EXPORT_JOBS: Final = Counter(
    "erp_export_jobs_total",
    "Background export lifecycle events",
    labelnames=("event",),
    registry=REGISTRY,
)
EXPORT_ROWS: Final = Counter(
    "erp_export_rows_total",
    "Rows written to background export files",
    registry=REGISTRY,
)

# ------------------------------------------------------------------ Phase 7
QUERY_RESULT_ROWS: Final = Histogram(
    "erp_query_result_rows",
    "Rows returned per executed analytics query (cache hits excluded)",
    labelnames=("connector",),
    buckets=(0, 1, 10, 50, 100, 500, 1000, 5000, 10_000, 100_000),
    registry=REGISTRY,
)
CONNECTOR_ERRORS: Final = Counter(
    "erp_connector_errors_total",
    "Connector failures by connector type and classified error code",
    labelnames=("connector", "code"),
    registry=REGISTRY,
)
QUESTION_GUARD_REFUSALS: Final = Counter(
    "erp_question_guard_refusals_total",
    "Questions refused by the deterministic guard before any model call",
    labelnames=("rule",),
    registry=REGISTRY,
)
