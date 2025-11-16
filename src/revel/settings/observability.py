"""Observability settings for Revel.

Configures:
- Structlog (structured logging with JSON output for Loki)
- OpenTelemetry (distributed tracing for Tempo)
- Prometheus (metrics collection)
- Pyroscope (continuous profiling - DISABLED due to SDK incompatibility)
"""

import re
import typing as t

import structlog
from decouple import config

from .base import VERSION

# Observability toggle
ENABLE_OBSERVABILITY = config("ENABLE_OBSERVABILITY", default=True, cast=bool)

# Sampling configuration
TRACING_SAMPLE_RATE = config(
    "TRACING_SAMPLE_RATE", default=1.0 if config("DEBUG", default=False, cast=bool) else 0.1, cast=float
)

# Service identification
SERVICE_NAME = config("SERVICE_NAME", default="revel")
SERVICE_VERSION = VERSION
DEPLOYMENT_ENVIRONMENT = config(
    "DEPLOYMENT_ENVIRONMENT", default="development" if config("DEBUG", default=False, cast=bool) else "production"
)

# OpenTelemetry configuration
OTEL_EXPORTER_OTLP_ENDPOINT = config("OTEL_EXPORTER_OTLP_ENDPOINT", default="http://localhost:4318")
OTEL_EXPORTER_OTLP_PROTOCOL = config("OTEL_EXPORTER_OTLP_PROTOCOL", default="http/protobuf")

# Pyroscope configuration (DISABLED - SDK incompatible with Grafana Pyroscope 1.6+)
# PYROSCOPE_SERVER_ADDRESS = config("PYROSCOPE_SERVER_ADDRESS", default="http://localhost:4040")
# PYROSCOPE_APPLICATION_NAME = f"{SERVICE_NAME}.{DEPLOYMENT_ENVIRONMENT}"

# Prometheus configuration
PROMETHEUS_EXPORT_MIGRATIONS = False  # Don't export migration metrics


# Structlog configuration
def scrub_pii(logger: t.Any, method_name: str, event_dict: dict[str, t.Any]) -> dict[str, t.Any]:
    """Scrub PII from log events.

    Redacts sensitive fields like passwords, card numbers, SSN, etc.
    """
    # Fields to completely redact
    sensitive_keys = [
        "password",
        "password2",
        "password_confirmation",
        "old_password",
        "new_password",
        "card_number",
        "cvv",
        "card_cvc",
        "cvc",
        "ssn",
        "social_security_number",
        "social_security",
        "secret",
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
    ]

    # Recursively scrub nested dicts
    def _scrub_dict(d: t.Any) -> dict[str, t.Any]:
        if not isinstance(d, dict):
            return t.cast(dict[str, t.Any], d)

        for key in list(d.keys()):
            # Check if key matches sensitive pattern
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                d[key] = "[REDACTED]"
            # Recursively scrub nested dicts
            elif isinstance(d[key], dict):
                d[key] = _scrub_dict(d[key])
            # Scrub email patterns from string values
            elif isinstance(d[key], str):
                # Only scrub emails from non-email fields
                if "email" not in key.lower():
                    d[key] = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", "[EMAIL]", d[key])

        return t.cast(dict[str, t.Any], d)

    return _scrub_dict(event_dict)


def add_app_context(logger: t.Any, method_name: str, event_dict: dict[str, t.Any]) -> dict[str, t.Any]:
    """Add application-level context to all log events."""
    event_dict["service"] = SERVICE_NAME
    event_dict["version"] = SERVICE_VERSION
    event_dict["environment"] = DEPLOYMENT_ENVIRONMENT
    return event_dict


# Structlog processors for direct use
STRUCTLOG_PROCESSORS = [
    structlog.contextvars.merge_contextvars,  # Merge context variables
    structlog.stdlib.add_logger_name,  # Add logger name
    structlog.stdlib.add_log_level,  # Add log level
    structlog.stdlib.PositionalArgumentsFormatter(),  # Format positional args
    structlog.processors.TimeStamper(fmt="iso"),  # Add ISO timestamp
    structlog.processors.StackInfoRenderer(),  # Render stack info
    structlog.processors.format_exc_info,  # Format exceptions
    structlog.processors.UnicodeDecoder(),  # Decode unicode
    add_app_context,  # Add service/version/environment
    scrub_pii,  # Scrub PII before serialization
    structlog.processors.JSONRenderer(),  # Render as JSON for Loki
]

# Processors for foreign loggers (Django, Celery, etc.)
FOREIGN_PRE_CHAIN = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    add_app_context,
    scrub_pii,
]

# Structlog configuration
structlog.configure(
    processors=STRUCTLOG_PROCESSORS,  # type: ignore[arg-type]
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)


# Loki configuration
LOKI_URL = config("LOKI_URL", default="http://localhost:3100")

# Django logging configuration (send to structlog + Loki)
LOGGING_HANDLERS: dict[str, dict[str, t.Any]] = {
    "console": {
        "class": "logging.StreamHandler",
        "formatter": "json",
    },
}

# Add Loki handler if observability is enabled
# Use QueueHandler to prevent blocking on HTTP requests to Loki
if ENABLE_OBSERVABILITY:
    # Background Loki handler (runs in separate thread via QueueListener)
    LOGGING_HANDLERS["loki"] = {
        "class": "logging_loki.LokiHandler",
        "url": f"{LOKI_URL}/loki/api/v1/push",
        "tags": {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "environment": DEPLOYMENT_ENVIRONMENT,
        },
        "version": "1",
    }

    # Queue handler for async logging (non-blocking)
    LOGGING_HANDLERS["queue"] = {
        "class": "logging.handlers.QueueHandler",
        "queue": {
            "()": "queue.Queue",
            "maxsize": 10000,  # Drop logs if queue fills (prevents memory exhaustion)
        },
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": FOREIGN_PRE_CHAIN,
        },
    },
    "handlers": LOGGING_HANDLERS,
    "root": {
        # Use queue handler instead of direct Loki handler (non-blocking)
        "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
        "level": "INFO" if config("DEBUG", default=False, cast=bool) else "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "WARNING",  # Only log slow queries/errors
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "urllib3": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "WARNING",  # Reduce noise from HTTP libraries
            "propagate": False,
        },
        # Reduce noise from verbose libraries
        "silk.middleware": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "WARNING",  # Silk is very noisy - only warnings/errors
            "propagate": False,
        },
        "silk.model_factory": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "ERROR",  # Even noisier - only errors
            "propagate": False,
        },
        "asyncio": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "multipart": {
            "handlers": ["console", "queue"] if ENABLE_OBSERVABILITY else ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
