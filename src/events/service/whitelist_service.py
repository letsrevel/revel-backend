"""Service layer for whitelist management.

This module provides functions for managing whitelist requests,
which allow users to be cleared despite fuzzy-matching blacklist entries.

The WhitelistRequest model serves as both the request workflow and the
whitelist itself - an APPROVED request means the user is whitelisted.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Blacklist, Organization, WhitelistRequest
from notifications.enums import NotificationType
from notifications.signals import notification_requested


def is_user_whitelisted(user: RevelUser, organization: Organization) -> bool:
    """Check if a user is whitelisted for an organization.

    A user is whitelisted if they have an APPROVED whitelist request.

    Args:
        user: The user to check
        organization: The organization to check against

    Returns:
        True if user is whitelisted, False otherwise
    """
    return WhitelistRequest.objects.filter(
        organization=organization,
        user=user,
        status=WhitelistRequest.Status.APPROVED,
    ).exists()


def get_whitelist_request(
    user: RevelUser,
    organization: Organization,
) -> WhitelistRequest | None:
    """Get the user's whitelist request for an organization.

    Args:
        user: The user
        organization: The organization

    Returns:
        WhitelistRequest if exists, None otherwise
    """
    return WhitelistRequest.objects.filter(
        organization=organization,
        user=user,
    ).first()


@transaction.atomic
def create_whitelist_request(
    user: RevelUser,
    organization: Organization,
    matched_entries: list[Blacklist],
    message: str = "",
) -> WhitelistRequest:
    """Create a whitelist request for a user.

    Args:
        user: The user requesting whitelist
        organization: The organization to request whitelist for
        matched_entries: The blacklist entries that triggered this request
        message: Optional message explaining why they should be whitelisted

    Returns:
        The created WhitelistRequest

    Raises:
        HttpError: If user already has a pending request or is already whitelisted
    """
    # Check if already whitelisted (has an APPROVED request)
    if is_user_whitelisted(user, organization):
        raise HttpError(400, str(_("You are already whitelisted for this organization.")))

    # Check if pending request already exists
    if WhitelistRequest.objects.filter(
        organization=organization,
        user=user,
        status=WhitelistRequest.Status.PENDING,
    ).exists():
        raise HttpError(400, str(_("You already have a pending whitelist request.")))

    # Create request
    request = WhitelistRequest.objects.create(
        organization=organization,
        user=user,
        message=message,
    )

    # Add matched entries
    request.matched_blacklist_entries.set(matched_entries)

    # Send notification to org admins
    def send_notification() -> None:
        from notifications.service.eligibility import get_staff_for_notification

        staff = get_staff_for_notification(
            organization.id,
            NotificationType.WHITELIST_REQUEST_CREATED,
        )

        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        for staff_member in staff:
            notification_requested.send(
                sender=WhitelistRequest,
                user=staff_member,
                notification_type=NotificationType.WHITELIST_REQUEST_CREATED,
                context={
                    "request_id": str(request.id),
                    "organization_id": str(organization.id),
                    "organization_name": organization.name,
                    "requester_id": str(user.id),
                    "requester_name": user.get_display_name(),
                    "requester_email": user.email,
                    "request_message": message,
                    "matched_entries_count": len(matched_entries),
                    "frontend_url": f"{frontend_base_url}/org/{organization.slug}/admin/blacklist",
                },
            )

    transaction.on_commit(send_notification)

    return request


@transaction.atomic
def approve_whitelist_request(
    request: WhitelistRequest,
    decided_by: RevelUser,
) -> WhitelistRequest:
    """Approve a whitelist request.

    Updates the request status to APPROVED, which grants the user whitelist access.

    Args:
        request: The WhitelistRequest to approve
        decided_by: The user approving the request

    Returns:
        The updated WhitelistRequest

    Raises:
        HttpError: If request is not pending
    """
    if request.status != WhitelistRequest.Status.PENDING:
        raise HttpError(400, str(_("This request is not pending.")))

    # Update request
    request.status = WhitelistRequest.Status.APPROVED
    request.decided_by = decided_by
    request.decided_at = timezone.now()
    request.save(update_fields=["status", "decided_by", "decided_at"])

    # Send notification to user
    def send_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        notification_requested.send(
            sender=WhitelistRequest,
            user=request.user,
            notification_type=NotificationType.WHITELIST_REQUEST_APPROVED,
            context={
                "organization_id": str(request.organization_id),
                "organization_name": request.organization.name,
                "frontend_url": f"{frontend_base_url}/org/{request.organization.slug}",
            },
        )

    transaction.on_commit(send_notification)

    return request


@transaction.atomic
def reject_whitelist_request(
    request: WhitelistRequest,
    decided_by: RevelUser,
) -> WhitelistRequest:
    """Reject a whitelist request.

    Args:
        request: The WhitelistRequest to reject
        decided_by: The user rejecting the request

    Returns:
        The updated WhitelistRequest

    Raises:
        HttpError: If request is not pending
    """
    if request.status != WhitelistRequest.Status.PENDING:
        raise HttpError(400, str(_("This request is not pending.")))

    request.status = WhitelistRequest.Status.REJECTED
    request.decided_by = decided_by
    request.decided_at = timezone.now()
    request.save(update_fields=["status", "decided_by", "decided_at"])

    # Send notification to user
    def send_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        notification_requested.send(
            sender=WhitelistRequest,
            user=request.user,
            notification_type=NotificationType.WHITELIST_REQUEST_REJECTED,
            context={
                "organization_id": str(request.organization_id),
                "organization_name": request.organization.name,
                "frontend_url": f"{frontend_base_url}/organizations",
            },
        )

    transaction.on_commit(send_notification)

    return request


def remove_from_whitelist(request: WhitelistRequest) -> None:
    """Remove a user from the whitelist by deleting their approved request.

    After deletion, if the user still fuzzy-matches blacklist entries,
    they will be prompted to submit a new whitelist request.

    Args:
        request: The WhitelistRequest to remove (must be APPROVED)

    Raises:
        HttpError: If request is not approved
    """
    if request.status != WhitelistRequest.Status.APPROVED:
        raise HttpError(400, str(_("Only approved requests can be removed from whitelist.")))
    request.delete()
