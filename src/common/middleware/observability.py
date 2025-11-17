"""Observability middleware for context enrichment."""

import typing as t
import uuid

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from opentelemetry import trace


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
        if not settings.ENABLE_OBSERVABILITY:
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
            "ip_address": self._get_client_ip(request),
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

        # Process request
        response = self.get_response(request)

        # Add request_id to response headers for client-side correlation
        response["X-Request-ID"] = request_id

        # Clear context after request
        structlog.contextvars.clear_contextvars()

        return response

    def _get_client_ip(self, request: HttpRequest) -> str:
        """Extract client IP address from request.

        Checks X-Forwarded-For header first (for proxied requests),
        then falls back to REMOTE_ADDR.

        Args:
            request: Django HttpRequest

        Returns:
            str: Client IP address
        """
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            # X-Forwarded-For can contain multiple IPs, take the first
            return str(x_forwarded_for.split(",")[0].strip())
        return str(request.META.get("REMOTE_ADDR", "unknown"))
