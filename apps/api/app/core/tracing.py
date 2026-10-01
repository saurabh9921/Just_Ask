"""OpenTelemetry tracing bootstrap.

Tracing is opt-in (``OTEL_ENABLED=true``) and exports via OTLP/HTTP to any
OpenTelemetry-compatible collector (Tempo, Jaeger, Honeycomb, ...). When disabled
the OpenTelemetry API remains a no-op, so ``get_tracer`` is always safe to call.

Spans must never carry SQL parameter values, prompts or result rows as attributes.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)
_configured = False


def configure_tracing(settings: Settings) -> TracerProvider | None:
    global _configured  # noqa: PLW0603 - process-wide, set once
    if not settings.otel_enabled or _configured:
        return None
    if not settings.otel_exporter_otlp_endpoint:
        logger.warning(
            "OTEL_ENABLED is true but OTEL_EXPORTER_OTLP_ENDPOINT is unset; tracing disabled"
        )
        return None

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": settings.app_version,
                "deployment.environment": settings.app_env.value,
            }
        )
    )
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("tracing enabled", extra={"otlp_endpoint_host": endpoint.split("/")[2]})
    return provider


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
