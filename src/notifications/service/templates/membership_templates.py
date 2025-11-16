"""Templates for membership-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class MembershipGrantedTemplate(NotificationTemplate):
    """Template for MEMBERSHIP_GRANTED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        role = notification.context.get("role", "member")
        return _("You're now a %(role)s of %(org)s") % {"role": role, "org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Welcome to %(org)s") % {"org": org_name}


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
register_template(NotificationType.MEMBERSHIP_REQUEST_CREATED, MembershipRequestCreatedTemplate())
register_template(NotificationType.MEMBERSHIP_REQUEST_APPROVED, MembershipRequestApprovedTemplate())
register_template(NotificationType.MEMBERSHIP_REQUEST_REJECTED, MembershipRequestRejectedTemplate())
