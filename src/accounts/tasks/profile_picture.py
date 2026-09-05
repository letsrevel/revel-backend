"""Pull the IdP ``picture`` claim into ``RevelUser.profile_picture`` after an OIDC account creation."""

import structlog
from celery import shared_task

from accounts.models import RevelUser
from accounts.service.oidc import fetch_profile_picture

logger = structlog.get_logger(__name__)


@shared_task(name="accounts.fetch_oidc_profile_picture")
def fetch_oidc_profile_picture(*, user_id: str, url: str) -> None:
    """Best-effort download of the IdP ``picture`` claim for a freshly created account."""
    user = RevelUser.objects.filter(id=user_id).first()
    if user is None:
        logger.warning("oidc_picture_user_missing", user_id=user_id)
        return
    fetch_profile_picture(user, url)
