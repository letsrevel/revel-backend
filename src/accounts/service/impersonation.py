"""Impersonation service layer.

Handles admin impersonation of users for debugging and customer support.
"""

from dataclasses import dataclass
from uuid import uuid4

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed

from accounts.jwt import (
    blacklist as blacklist_token,
)
from accounts.jwt import (
    check_blacklist,
    create_impersonation_access_token,
    create_impersonation_request_token,
    validate_impersonation_request_token,
)
from accounts.models import ImpersonationLog, RevelUser

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ImpersonationResult:
    """Result of a successful impersonation token redemption."""

    access_token: str
    expires_in: int
    user: RevelUser
    admin_email: str


def can_impersonate(admin: RevelUser, target: RevelUser) -> tuple[bool, str | None]:
    """Check if admin can impersonate target user.

    Args:
        admin: The user attempting to impersonate.
        target: The user to be impersonated.

    Returns:
        Tuple of (allowed, error_message). If allowed is False,
        error_message contains the reason.
    """
    if not admin.is_superuser:
        return False, str(_("Only superusers can impersonate users."))
    # Check self-impersonation before other checks for clearer error message
    if admin.id == target.id:
        return False, str(_("Cannot impersonate yourself."))
    if target.is_superuser:
        return False, str(_("Cannot impersonate other superusers."))
    if target.is_staff:
        return False, str(_("Cannot impersonate staff members."))
    if not target.is_active:
        return False, str(_("Cannot impersonate inactive users."))
    return True, None


@transaction.atomic
def create_impersonation_request(
    admin: RevelUser,
    target: RevelUser,
    ip_address: str | None = None,
    user_agent: str = "",
) -> tuple[str, ImpersonationLog]:
    """Generate an impersonation request token and create audit log.

    Args:
        admin: The superuser initiating impersonation.
        target: The user to be impersonated.
        ip_address: IP address from which request originated.
        user_agent: Browser/client user agent string.

    Returns:
        Tuple of (token, impersonation_log).

    Raises:
        HttpError: If impersonation is not allowed.
    """
    # Validate permissions
    allowed, error = can_impersonate(admin, target)
    if not allowed:
        logger.warning(
            "impersonation_denied",
            admin_id=str(admin.id),
            admin_email=admin.email,
            target_id=str(target.id),
            target_email=target.email,
            reason=error,
        )
        raise HttpError(403, error or "Impersonation not allowed.")

    # Generate unique JTI for tracking
    jti = uuid4().hex

    # Create audit log entry
    log = ImpersonationLog.objects.create(
        admin_user=admin,
        target_user=target,
        ip_address=ip_address,
        user_agent=user_agent,
        token_jti=jti,
    )

    # Generate request token
    token = create_impersonation_request_token(
        admin_user_id=str(admin.id),
        target_user_id=str(target.id),
        jti=jti,
    )

    logger.info(
        "impersonation_request_created",
        admin_id=str(admin.id),
        admin_email=admin.email,
        target_id=str(target.id),
        target_email=target.email,
        log_id=str(log.id),
        ip_address=ip_address,
    )

    return token, log


@transaction.atomic
def redeem_impersonation_token(token: str) -> ImpersonationResult:
    """Validate and redeem an impersonation request token.

    Exchanges the one-time request token for an access token.

    Args:
        token: The impersonation request token.

    Returns:
        ImpersonationResult with access token and user info.

    Raises:
        AuthenticationFailed: If token is invalid, expired, or already used.
        HttpError: If users not found or impersonation no longer allowed.
    """
    # Validate token signature and structure
    payload = validate_impersonation_request_token(token)

    # Check if token is blacklisted
    check_blacklist(payload.jti)

    # Find the impersonation log entry
    try:
        log = ImpersonationLog.objects.select_related("admin_user", "target_user").get(token_jti=payload.jti)
    except ImpersonationLog.DoesNotExist as e:
        logger.warning(
            "impersonation_log_not_found",
            jti=payload.jti,
        )
        raise AuthenticationFailed("Invalid impersonation token.") from e

    # Check if already redeemed
    if log.is_redeemed:
        logger.warning(
            "impersonation_token_already_redeemed",
            log_id=str(log.id),
            jti=payload.jti,
            redeemed_at=str(log.redeemed_at),
        )
        raise AuthenticationFailed("Impersonation token has already been used.")

    # Re-validate permissions (in case user status changed)
    allowed, error = can_impersonate(log.admin_user, log.target_user)
    if not allowed:
        logger.warning(
            "impersonation_no_longer_allowed",
            log_id=str(log.id),
            admin_id=str(log.admin_user.id),
            target_id=str(log.target_user.id),
            reason=error,
        )
        raise HttpError(403, error or "Impersonation no longer allowed.")

    # Blacklist the request token (single-use)
    blacklist_token(token)

    # Mark as redeemed
    log.redeemed_at = timezone.now()
    log.save(update_fields=["redeemed_at"])

    # Generate access token with impersonation claims
    access_token = create_impersonation_access_token(
        user=log.target_user,
        admin_user=log.admin_user,
        impersonation_log_id=str(log.id),
    )

    # Calculate expires_in from settings
    expires_in = int(settings.IMPERSONATION_ACCESS_TOKEN_LIFETIME.total_seconds())

    logger.info(
        "impersonation_token_redeemed",
        log_id=str(log.id),
        admin_id=str(log.admin_user.id),
        admin_email=log.admin_user.email,
        target_id=str(log.target_user.id),
        target_email=log.target_user.email,
    )

    return ImpersonationResult(
        access_token=access_token,
        expires_in=expires_in,
        user=log.target_user,
        admin_email=log.admin_user.email,
    )
