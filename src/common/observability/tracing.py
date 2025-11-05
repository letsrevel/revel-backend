"""OpenTelemetry distributed tracing setup."""

import logging

from django.conf import settings
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

logger = logging.getLogger(__name__)


def init_tracing() -> None:
    """Initialize OpenTelemetry distributed tracing.

    Sets up:
    - TracerProvider with resource attributes
    - OTLP exporter to Tempo
    - Sampling based on environment
    - Auto-instrumentation for Django, Celery, PostgreSQL, Redis
    """
    if not settings.ENABLE_OBSERVABILITY:
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
        DjangoInstrumentor().instrument()
        CeleryInstrumentor().instrument()
        PsycopgInstrumentor().instrument()
        RedisInstrumentor().instrument()
        logger.info(
            f"OpenTelemetry tracing initialized: service={settings.SERVICE_NAME}, "
            f"sample_rate={settings.TRACING_SAMPLE_RATE}, endpoint={settings.OTEL_EXPORTER_OTLP_ENDPOINT}"
        )
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry tracing: {e}", exc_info=True)
