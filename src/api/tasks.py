"""Tasks for tracking internal errors."""

import base64
import hashlib
import typing as t
from datetime import timedelta

import structlog
from celery import shared_task
from django.utils.timezone import now
from ninja_jwt.token_blacklist.models import OutstandingToken
from ninja_jwt.utils import aware_utcnow

from .models import Error, ErrorOccurrence

logger = structlog.get_logger(__name__)


@shared_task
def track_internal_error(
    path: str,
    traceback_str: str,
    encoded_payload: str | None = None,
    json_payload: dict[str, t.Any] | None = None,
    metadata: dict[str, t.Any] | None = None,
) -> None:  # pragma: no cover
    """Track an internal error."""
    to_hash = path + traceback_str
    md5 = hashlib.md5(to_hash.encode()).hexdigest()

    error, created = Error.objects.get_or_create(
        md5=md5,
        defaults={
            "path": path,
            "traceback": traceback_str,
            "payload": base64.b64decode(encoded_payload) if encoded_payload else None,
            "json_payload": json_payload if json_payload else None,
            "request_metadata": metadata,
            "created_at": now(),
        },
    )
    ErrorOccurrence.objects.create(signature=error)


@shared_task
def clear_data_from_old_errors() -> None:
    """Clear data from old errors."""
    from common.models import SiteSettings

    site_settings = SiteSettings.get_solo()
    data_retention_days = site_settings.data_retention_days
    qs = Error.objects.filter(created_at__lte=now() - timedelta(days=data_retention_days))
    qs.update(payload=None, json_payload=None, request_metadata=None)


@shared_task
def flush_expired_tokens() -> None:
    """Flushes any expired tokens in the outstanding token list.

    This task is designed to be run periodically to clean up expired tokens.
    """
    # Get the current time in UTC
    current_time = aware_utcnow()

    # Delete expired tokens
    OutstandingToken.objects.filter(expires_at__lte=current_time).delete()
