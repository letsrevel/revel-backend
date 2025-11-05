"""Tasks for tracking internal errors."""

import structlog
from celery import shared_task
from ninja_jwt.token_blacklist.models import OutstandingToken
from ninja_jwt.utils import aware_utcnow

logger = structlog.get_logger(__name__)


@shared_task
def flush_expired_tokens() -> None:
    """Flushes any expired tokens in the outstanding token list.

    This task is designed to be run periodically to clean up expired tokens.
    """
    # Get the current time in UTC
    current_time = aware_utcnow()

    # Delete expired tokens
    OutstandingToken.objects.filter(expires_at__lte=current_time).delete()
