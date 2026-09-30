"""OpenTelemetry distributed tracing setup."""

import re
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
# Path segments that carry credentials: org/event invitation tokens
# (``/claim-invitation/{token}``) and the integrations webhook secret
# (``/{provider}/webhook/{secret}``, where the path *is* the authentication).
_SECRET_PATH_SEGMENT = re.compile(r"(?<=/claim-invitation/)[^/?#]+|(?<=/webhook/)[^/?#]+")
# Span attributes (old and new HTTP semconv) that can hold request URL data; ``url.path`` is set
# under OTEL_SEMCONV_STABILITY_OPT_IN=http / http/dup and carries path secrets.
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.query", "url.path")


def redact_url_value(value: str, *, bare_query: bool = False) -> str:
    """Replace credential query-param values and secret path segments with REDACTED.

    Args:
        value: A full URL, a path with query string, or (``bare_query``) just the query string.
        bare_query: ``value`` is a query string without the leading "?" (the ``url.query``
            attribute), so a literal "?" inside it must not be treated as the separator.

    Returns:
        ``value`` with the values of ``REDACTED_QUERY_PARAMS`` and the segments matched by
        ``_SECRET_PATH_SEGMENT`` replaced by ``REDACTED``.
    """
    if bare_query:
        base, sep, query = "", "", value
    else:
        value = _SECRET_PATH_SEGMENT.sub("REDACTED", value)
        base, sep, query = value.partition("?")
        if not sep:  # no query at all, or a bare query string passed without the flag
            base, query = "", value
    pairs = []
    for pair in query.split("&"):
        name, has_value, _ = pair.partition("=")
        if has_value and unquote_plus(name) in REDACTED_QUERY_PARAMS:
            pair = f"{name}=REDACTED"
        pairs.append(pair)
    return f"{base}{sep}{'&'.join(pairs)}"


def redact_request_span(span: trace.Span, request: HttpRequest) -> None:
    """DjangoInstrumentor request_hook: scrub credentials from the server span's URL attributes."""
    attributes = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if not isinstance(value, str):
            continue
        redacted = redact_url_value(value, bare_query=key == "url.query")
        if redacted != value:
            span.set_attribute(key, redacted)


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
