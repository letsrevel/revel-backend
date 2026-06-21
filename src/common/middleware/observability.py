"""Observability middleware for context enrichment."""

import time
import traceback
import typing as t
import uuid

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from opentelemetry import trace

from common.client_ip import get_client_ip

logger = structlog.get_logger("common.middleware.observability")

_SKIP_LOG_PATHS: frozenset[str] = frozenset({"/metrics", "/health", "/api/healthcheck"})


class StructlogContextMiddleware:
    """Enriches structlog context with request metadata.

    Automatically binds request-level context (request_id, user_id, IP, etc.)
    to all log events during the request lifecycle.
    """

    def __init__(self, get_response: t.Callable[[HttpRequest], HttpResponse]) -> None:
        """Initialize middleware.

        Args:
            get_response: Django middleware get_response callable
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process request and bind context.

        Args:
            request: Django HttpRequest

        Returns:
            HttpResponse
        """
        if not settings.FEATURE_OBSERVABILITY:
            return self.get_response(request)

        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Clear any previous context
        structlog.contextvars.clear_contextvars()

        # Get current trace context (if tracing is active)
        trace_id = None
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            # Format trace_id as hex string (16 bytes -> 32 hex chars)
            trace_id = format(span.get_span_context().trace_id, "032x")

        # Bind request context
        context: dict[str, t.Any] = {
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "ip_address": get_client_ip(request) or "unknown",
        }

        # Add trace_id if available (for log-to-trace correlation)
        if trace_id:
            context["trace_id"] = trace_id

        # Add user context if authenticated
        if hasattr(request, "user") and request.user.is_authenticated:
            context["user_id"] = str(request.user.id)

        # Add endpoint name if available
        if hasattr(request, "resolver_match") and request.resolver_match:
            context["endpoint"] = request.resolver_match.view_name

        # Add organization context if available (from token or user membership)
        if hasattr(request, "organization") and request.organization:
            context["organization_id"] = str(request.organization.id)

        structlog.contextvars.bind_contextvars(**context)

        # Process request. If anything outside the Ninja API (Django admin,
        # non-API views, middleware) raises, Django swallows the traceback into
        # a generic 500 — log it here so we always have a stacktrace tied to
        # the request_id/trace_id context bound above.
        start_time = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception as exc:
            logger.error(
                "unhandled_exception",
                exc_info=exc,
                traceback="".join(traceback.format_exception(exc)),
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                method=request.method,
                path=request.path,
            )
            structlog.contextvars.clear_contextvars()
            raise

        # Add request_id to response headers for client-side correlation
        response["X-Request-ID"] = request_id

        # Emit structured request completion log
        if request.path not in _SKIP_LOG_PATHS:
            logger.info(
                "request_finished",
                status_code=response.status_code,
                response_time_ms=round((time.monotonic() - start_time) * 1000, 2),
                # content_length is 0 for streaming responses (no Content-Length header, no .content)
                content_length=int(
                    response.get("Content-Length") or (len(response.content) if hasattr(response, "content") else 0)
                ),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )

        # Clear context after request
        structlog.contextvars.clear_contextvars()

        return response
