"""Credential-bearing query params never reach exported OpenTelemetry spans (#1042)."""

import typing as t
from unittest import mock

import pytest
from django.test import Client, override_settings
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from common.observability import tracing


@pytest.fixture
def span_exporter() -> t.Iterator[InMemorySpanExporter]:
    """Instrument Django exactly as ``init_tracing`` does, exporting to memory."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    DjangoInstrumentor().instrument(tracer_provider=provider, request_hook=tracing.redact_request_span)
    try:
        yield exporter
    finally:
        DjangoInstrumentor().uninstrument()


def _exported_values(exporter: InMemorySpanExporter) -> list[str]:
    spans = exporter.get_finished_spans()
    assert spans, "the instrumented request produced no span"
    return [str(value) for span in spans for value in (span.attributes or {}).values()]


@pytest.mark.django_db
@pytest.mark.parametrize("param", sorted(tracing.REDACTED_QUERY_PARAMS))
def test_credential_query_param_is_redacted_from_exported_spans(
    span_exporter: InMemorySpanExporter, param: str
) -> None:
    # REQUEST_URI mimics gunicorn, which is what makes the WSGI instrumentation record the raw
    # target (query string included) as ``http.target`` in production.
    Client().get(
        f"/api/nope?{param}=s3cr3t-value&x=1",
        REQUEST_URI=f"/api/nope?{param}=s3cr3t-value&x=1",
    )

    values = _exported_values(span_exporter)
    assert not any("s3cr3t-value" in value for value in values)
    assert f"/api/nope?{param}=REDACTED&x=1" in values


@pytest.mark.django_db
def test_non_credential_query_is_left_untouched(span_exporter: InMemorySpanExporter) -> None:
    Client().get("/api/nope?page=2&x=1", REQUEST_URI="/api/nope?page=2&x=1")

    assert "/api/nope?page=2&x=1" in _exported_values(span_exporter)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/p?token=abc&x=1", "/p?token=REDACTED&x=1"),
        ("https://h/p?x=1&sig=abc&exp=9", "https://h/p?x=1&sig=REDACTED&exp=9"),
        ("token=abc&x=1", "token=REDACTED&x=1"),  # url.query carries the bare query string
        ("/p?code=a&state=b", "/p?code=REDACTED&state=REDACTED"),
        ("/p?x=1", "/p?x=1"),
        ("/p", "/p"),
        ("/p?to%6Ben=abc", "/p?to%6Ben=REDACTED"),  # percent-encoded name
        ("/p?token", "/p?token"),  # no value, nothing to leak
    ],
)
def test_redact_url_value(value: str, expected: str) -> None:
    assert tracing.redact_url_value(value) == expected


@override_settings(FEATURE_OBSERVABILITY=True)
def test_init_tracing_wires_the_redaction_hook() -> None:
    with (
        mock.patch.object(tracing, "DjangoInstrumentor") as django_instrumentor,
        mock.patch.object(tracing, "CeleryInstrumentor"),
        mock.patch.object(tracing, "PsycopgInstrumentor"),
        mock.patch.object(tracing, "RedisInstrumentor"),
        mock.patch.object(tracing, "OTLPSpanExporter"),
        mock.patch("common.observability.tracing.trace.set_tracer_provider"),
    ):
        tracing.init_tracing()

    django_instrumentor.return_value.instrument.assert_called_once_with(request_hook=tracing.redact_request_span)
