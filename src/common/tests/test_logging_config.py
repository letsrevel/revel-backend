"""Regression tests for the production logging configuration.

These lock in two related observability guarantees that are easy to break with a
one-line settings edit:

1. **#480 — every 500 keeps its traceback.** Django logs unhandled exceptions in
   admin / non-API views to the stdlib ``django.request`` logger with ``exc_info``.
   Our ``json`` formatter (a structlog ``ProcessorFormatter`` with ``format_exc_info``
   in its ``foreign_pre_chain``) must serialize that traceback into the JSON line so it
   reaches Loki. Without an error tracker (Sentry/GlitchTip — deliberately not added;
   Loki + Grafana alerting is the chosen sink), this rendering is the *only* thing that
   makes production 500s debuggable.

2. **4xx log-noise reduction.** ``django.request`` is configured at ``ERROR`` (not
   ``WARNING``) so it no longer duplicates our structured ``request_finished`` middleware
   log for every 4xx. Django's ``log_response`` emits 5xx at ERROR (kept) and 4xx at
   WARNING (dropped); django-ninja-extra's access logger is also ``django.request``.
"""

import json
import logging
import sys
import typing as t

from django.conf import settings


def _build_json_formatter() -> logging.Formatter:
    """Instantiate the real ``json`` formatter from the configured LOGGING dict.

    Uses the exact ``processor`` + ``foreign_pre_chain`` from settings rather than a
    hand-rolled copy, so the test fails if either is changed in a way that drops
    traceback rendering.
    """
    logging_config = t.cast(dict[str, t.Any], settings.LOGGING)
    spec = logging_config["formatters"]["json"]
    factory = t.cast(t.Callable[..., logging.Formatter], spec["()"])
    return factory(processor=spec["processor"], foreign_pre_chain=spec["foreign_pre_chain"])


def test_django_request_error_with_exc_info_renders_traceback() -> None:
    """A ``django.request`` ERROR carrying ``exc_info`` serializes the full traceback (#480).

    Mirrors what Django's ``log_response`` emits for an unhandled admin/non-API 500.
    """
    formatter = _build_json_formatter()

    try:
        raise ValueError("boom in admin delete")
    except ValueError:
        record = logging.LogRecord(
            name="django.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Internal Server Error: /admin/accounts/reveluser/",
            args=(),
            exc_info=sys.exc_info(),
        )

    rendered = formatter.format(record)
    payload = json.loads(rendered)

    # The traceback lands in the structured ``exception`` field (format_exc_info), not
    # just the human message — this is the exact guarantee #420/#484 introduced.
    assert "exception" in payload, f"no traceback field in rendered log line: {payload}"
    assert "ValueError" in payload["exception"]
    assert "boom in admin delete" in payload["exception"]
    assert payload["event"] == "Internal Server Error: /admin/accounts/reveluser/"


def test_json_line_is_single_render() -> None:
    """The formatter emits one parseable JSON object, not a double-encoded string (#484)."""
    formatter = _build_json_formatter()
    record = logging.LogRecord(
        name="django.request",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Internal Server Error: /admin/",
        args=(),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    # A double-render regression would make ``event`` a JSON-encoded string instead of
    # the dict we assert on here.
    assert isinstance(payload, dict)
    assert payload["event"] == "Internal Server Error: /admin/"


def test_django_request_logger_configured_at_error_level() -> None:
    """``django.request`` is ERROR so redundant 4xx WARNINGs are dropped (noise fix)."""
    logging_config = t.cast(dict[str, t.Any], settings.LOGGING)
    assert logging_config["loggers"]["django.request"]["level"] == "ERROR"


def test_django_request_4xx_warning_is_filtered_out() -> None:
    """At ERROR level a 4xx WARNING (e.g. ``Not Found:``) never reaches a handler.

    Django applied ``settings.LOGGING`` via ``dictConfig`` at startup, so the live logger's
    effective level proves WARNING records are discarded before any handler runs.
    """
    logger = logging.getLogger("django.request")
    assert logger.getEffectiveLevel() == logging.ERROR
    assert not logger.isEnabledFor(logging.WARNING)
    assert logger.isEnabledFor(logging.ERROR)
