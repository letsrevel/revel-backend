"""OpenTelemetry distributed tracing setup."""

from urllib.parse import unquote_plus

import structlog
from django.conf import settings
from django.http import HttpRequest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

logger = structlog.get_logger(__name__)

# Query params that carry credentials (email-link tokens, signed-URL signatures, org/event
# access tokens, OAuth/OIDC callback code+state). Their values must never reach Tempo (#1042).
REDACTED_QUERY_PARAMS = frozenset({"token", "sig", "ot", "et", "code", "state"})
# Span attributes (old and new HTTP semconv) that can hold the raw query string.
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.query")


def redact_url_value(value: str) -> str:
    """Replace credential query-param values in a URL, target, or bare query string with REDACTED."""
    base, sep, query = value.partition("?")
    if not sep:  # ``url.query`` holds the query string without the leading "?"
        base, query = "", value
    pairs = []
    for pair in query.split("&"):
        name, has_value, _ = pair.partition("=")
        if has_value and unquote_plus(name) in REDACTED_QUERY_PARAMS:
            pair = f"{name}=REDACTED"
        pairs.append(pair)
    return f"{base}{sep}{'&'.join(pairs)}"


def redact_request_span(span: trace.Span, request: HttpRequest) -> None:
    """DjangoInstrumentor request_hook: scrub credential query params from the server span."""
    attributes = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and "=" in value:
            span.set_attribute(key, redact_url_value(value))


def init_tracing() -> None:
    """Initialize OpenTelemetry distributed tracing.

    Sets up:
    - TracerProvider with resource attributes
    - OTLP exporter to Tempo
    - Sampling based on environment
    - Auto-instrumentation for Django, Celery, PostgreSQL, Redis
    """
    if not settings.FEATURE_OBSERVABILITY:
        logger.info("Observability disabled - skipping OpenTelemetry tracing initialization")
        return

    # Create resource with service metadata
    resource = Resource.create(
        {
            SERVICE_NAME: settings.SERVICE_NAME,
            SERVICE_VERSION: settings.SERVICE_VERSION,
            DEPLOYMENT_ENVIRONMENT: settings.DEPLOYMENT_ENVIRONMENT,
        }
    )

    # Create tracer provider with sampling
    sampler = ParentBasedTraceIdRatio(settings.TRACING_SAMPLE_RATE)
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=sampler,
    )

    # Configure OTLP exporter to Tempo
    otlp_exporter = OTLPSpanExporter(
        endpoint=f"{settings.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces",
    )

    # Add batch span processor (exports spans asynchronously)
    span_processor = BatchSpanProcessor(otlp_exporter)
    tracer_provider.add_span_processor(span_processor)

    # Set as global tracer provider
    trace.set_tracer_provider(tracer_provider)

    # Auto-instrument frameworks
    try:
        DjangoInstrumentor().instrument(request_hook=redact_request_span)
        CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]
        PsycopgInstrumentor().instrument()
        RedisInstrumentor().instrument()
        logger.info(
            f"OpenTelemetry tracing initialized: service={settings.SERVICE_NAME}, "
            f"sample_rate={settings.TRACING_SAMPLE_RATE}, endpoint={settings.OTEL_EXPORTER_OTLP_ENDPOINT}"
        )
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry tracing: {e}", exc_info=True)
