"""Templates for organization-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class OrgAnnouncementTemplate(NotificationTemplate):
    """Template for ORG_ANNOUNCEMENT notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        announcement_title = notification.context.get("announcement_title", "")
        return _("%(org)s: %(title)s") % {"org": org_name, "title": announcement_title}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        announcement_title = notification.context.get("announcement_title", "")
        return _("%(org)s - %(title)s") % {"org": org_name, "title": announcement_title}


class OrgContactMessageReceivedTemplate(NotificationTemplate):
    """Template for ORG_CONTACT_MESSAGE_RECEIVED notification (to org admins)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        sender_email = notification.context.get("sender_email", "")
        return _("New contact message for %(org)s from %(sender)s") % {"org": org_name, "sender": sender_email}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject (only used if a user opts EMAIL channel in)."""
        org_name = notification.context.get("organization_name", "")
        return _("New contact message: %(org)s") % {"org": org_name}


class OrgSetupNudgeTemplate(NotificationTemplate):
    """Template for ORG_SETUP_NUDGE (to the org owner); copy branches on ``context.trigger``.

    The ``check_in`` trigger is a personal, plain-text note (no HTML alternative, no button)
    so it reads like the founder wrote it, not like a campaign.
    """

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        return self.get_email_subject(notification)

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        params = {"org": ctx.get("organization_name", ""), "event": ctx.get("event_name", "")}
        match ctx.get("trigger"):
            case "draft_event":
                return _('Your draft "%(event)s" is still waiting') % params
            case "private_profile":
                return _("Only you can see %(org)s on Revel") % params
            case "no_events":
                return _("Your first event on %(org)s") % params
            case "check_in":
                return _("A quick question about %(org)s") % params
            case "dormant":
                return _("Planning the next %(org)s event?") % params
            case _:
                return _("A note about %(org)s") % params

    def get_email_html_body(self, notification: Notification) -> str | None:
        """Plain text only for the personal check-in; branded HTML otherwise."""
        if notification.context.get("trigger") == "check_in":
            return None
        return super().get_email_html_body(notification)


# Register templates
register_template(NotificationType.ORG_ANNOUNCEMENT, OrgAnnouncementTemplate())
register_template(NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, OrgContactMessageReceivedTemplate())
register_template(NotificationType.ORG_SETUP_NUDGE, OrgSetupNudgeTemplate())
