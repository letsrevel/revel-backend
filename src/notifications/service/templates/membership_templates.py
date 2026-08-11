"""Templates for membership-related notifications."""

import typing as t
from datetime import timedelta

import structlog
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template

logger = structlog.get_logger(__name__)

# How long an emailed signed pkpass link for a membership card keeps working.
MEMBERSHIP_SIGNED_LINK_TTL = timedelta(days=30)


def _add_membership_wallet_context(base_context: dict[str, t.Any], notification: Notification) -> dict[str, t.Any]:
    """Add membership-card badge URLs to a membership email template context.

    Sets ``apple_wallet_signed_url``, ``google_wallet_save_url`` and
    ``membership_pdf_url`` when available. Never raises — email rendering must
    not fail because a wallet link could not be built. CANCELLED/BANNED members
    get no links (mirrors ``MembershipWalletController.get_member()``).
    """
    from events.models import OrganizationMember
    from events.utils import apple_wallet_configured, google_wallet_configured

    member_id = notification.context.get("member_id") or None
    organization_id = notification.context.get("organization_id")
    member = None
    qs = OrganizationMember.objects.for_visibility().select_related("organization", "tier", "user")
    if member_id:
        member = qs.filter(id=member_id).first()
    elif organization_id:
        member = qs.filter(organization_id=organization_id, user=notification.user).first()
    if member is None:
        return base_context

    base_context["context"]["membership_pdf_url"] = (
        f"{settings.BASE_URL.rstrip('/')}/api/me/organizations/{member.organization.slug}/membership/pdf"
    )

    if google_wallet_configured():
        try:
            from wallet.google import service as google_wallet_service

            base_context["context"]["google_wallet_save_url"] = google_wallet_service.membership_save_url(member)
        except Exception:
            logger.exception("failed_to_generate_google_wallet_link", member_id=str(member.id))

    if apple_wallet_configured():
        try:
            from django.urls import reverse

            from common.signing import generate_signature

            expires = int((timezone.now() + MEMBERSHIP_SIGNED_LINK_TTL).timestamp())
            path = reverse("api:membership_apple_wallet_signed", kwargs={"member_id": member.id})
            sig = generate_signature(path, expires)
            base_context["context"]["apple_wallet_signed_url"] = (
                f"{settings.BASE_URL.rstrip('/')}{path}?exp={expires}&sig={sig}"
            )
        except Exception:
            logger.exception("failed_to_generate_apple_wallet_link", member_id=str(member.id))

    return base_context


class MembershipGrantedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_GRANTED notification."""

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Enrich the base context with membership wallet badge links."""
        return _add_membership_wallet_context(super()._get_template_context(notification), notification)

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        role = notification.context.get("role", "member")
        return _("You're now a %(role)s of %(org)s") % {"role": role, "org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Welcome to %(org)s") % {"org": org_name}


class MembershipCardUpdatedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_CARD_UPDATED notification (tier changed)."""

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Enrich the base context with membership wallet badge links."""
        return _add_membership_wallet_context(super()._get_template_context(notification), notification)

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Your membership card was updated: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        return _("Your membership card was updated")


class MembershipPromotedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_PROMOTED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        role = notification.context.get("role", "member")
        return _("You've been promoted to %(role)s in %(org)s") % {"role": role, "org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        role = notification.context.get("role", "member")
        return _("Role Updated: %(role)s - %(org)s") % {"role": role, "org": org_name}


class MembershipRemovedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_REMOVED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership Removed: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership Removed: %(org)s") % {"org": org_name}


class MembershipRequestApprovedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_REQUEST_APPROVED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership Request Approved: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Welcome to %(org)s - Request Approved") % {"org": org_name}


class MembershipRequestCreatedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_REQUEST_CREATED notification (to organizers)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        requester_name = notification.context.get("requester_name", "")
        org_name = notification.context.get("organization_name", "")
        return _("%(user)s requested to join %(org)s") % {"user": requester_name, "org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("New membership request: %(org)s") % {"org": org_name}


class MembershipRequestRejectedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_REQUEST_REJECTED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership Request Declined: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership Request Update: %(org)s") % {"org": org_name}


# Register templates
register_template(NotificationType.MEMBERSHIP_GRANTED, MembershipGrantedTemplate())
register_template(NotificationType.MEMBERSHIP_PROMOTED, MembershipPromotedTemplate())
register_template(NotificationType.MEMBERSHIP_REMOVED, MembershipRemovedTemplate())
register_template(NotificationType.MEMBERSHIP_CARD_UPDATED, MembershipCardUpdatedTemplate())
register_template(NotificationType.MEMBERSHIP_REQUEST_CREATED, MembershipRequestCreatedTemplate())
register_template(NotificationType.MEMBERSHIP_REQUEST_APPROVED, MembershipRequestApprovedTemplate())
register_template(NotificationType.MEMBERSHIP_REQUEST_REJECTED, MembershipRequestRejectedTemplate())
