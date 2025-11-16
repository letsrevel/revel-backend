"""Templates for ticket-related notifications."""

import base64
from typing import Any

from django.utils.translation import gettext as _

from events.models import Event, Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class TicketCreatedTemplate(NotificationTemplate):
    """Template for TICKET_CREATED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("New Ticket: %(holder)s - %(event)s") % {"holder": ticket_holder_name, "event": event_name}
        # Notification to ticket holder
        return _("Ticket Confirmation for %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("New Ticket: %(holder)s - %(event)s") % {"holder": ticket_holder_name, "event": event_name}
        # Notification to ticket holder
        return _("Your ticket for %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, Any]:
        """Get attachments (ticket PDF + ICS).

        Conditionally includes PDF based on ticket status and payment method.
        Always includes ICS calendar file.
        """
        ticket_id = notification.context.get("ticket_id")
        event_id = notification.context.get("event_id")
        include_pdf = notification.context.get("include_pdf", True)
        include_ics = notification.context.get("include_ics", True)

        if not ticket_id or not event_id:
            return {}

        attachments = {}

        try:
            # Generate ticket PDF if requested
            if include_pdf:
                ticket = Ticket.objects.select_related("event", "user", "tier").get(pk=ticket_id)
                from events.utils import create_ticket_pdf

                pdf_content = create_ticket_pdf(ticket)
                attachments["ticket.pdf"] = {
                    "content_base64": base64.b64encode(pdf_content).decode("utf-8"),
                    "mimetype": "application/pdf",
                }

            # Generate event ICS calendar file if requested
            if include_ics:
                event = Event.objects.select_related("city").get(pk=event_id)
                ics_content = event.ics()
                attachments["event.ics"] = {
                    "content_base64": base64.b64encode(ics_content).decode("utf-8"),
                    "mimetype": "text/calendar",
                }
        except (Ticket.DoesNotExist, Event.DoesNotExist):
            # Log error but don't fail the entire notification
            pass

        return attachments


class TicketUpdatedTemplate(NotificationTemplate):
    """Template for TICKET_UPDATED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        action = notification.context.get("action", "updated")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("Ticket %(action)s: %(holder)s - %(event)s") % {
                "action": action.capitalize(),
                "holder": ticket_holder_name,
                "event": event_name,
            }
        # Notification to ticket holder
        return _("Ticket Update for %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        action = notification.context.get("action", "updated")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("Ticket %(action)s: %(holder)s - %(event)s") % {
                "action": action.capitalize(),
                "holder": ticket_holder_name,
                "event": event_name,
            }
        # Notification to ticket holder
        return _("Ticket Update - %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, Any]:
        """Get attachments (ticket PDF + ICS).

        For ticket activations, include both PDF and ICS.
        For cancellations/refunds, no attachments are included.
        """
        ticket_id = notification.context.get("ticket_id")
        event_id = notification.context.get("event_id")
        include_pdf = notification.context.get("include_pdf", True)
        include_ics = notification.context.get("include_ics", True)

        # No attachments for cancellations/refunds (include_pdf and include_ics will be False)
        if not include_pdf and not include_ics:
            return {}

        if not ticket_id or not event_id:
            return {}

        attachments = {}

        try:
            # Generate ticket PDF if requested
            if include_pdf:
                ticket = Ticket.objects.select_related("event", "user", "tier").get(pk=ticket_id)
                from events.utils import create_ticket_pdf

                pdf_content = create_ticket_pdf(ticket)
                attachments["ticket.pdf"] = {
                    "content_base64": base64.b64encode(pdf_content).decode("utf-8"),
                    "mimetype": "application/pdf",
                }

            # Generate event ICS calendar file if requested
            if include_ics:
                event = Event.objects.select_related("city").get(pk=event_id)
                ics_content = event.ics()
                attachments["event.ics"] = {
                    "content_base64": base64.b64encode(ics_content).decode("utf-8"),
                    "mimetype": "text/calendar",
                }
        except (Ticket.DoesNotExist, Event.DoesNotExist):
            # Log error but don't fail the entire notification
            pass

        return attachments


class PaymentConfirmationTemplate(NotificationTemplate):
    """Template for PAYMENT_CONFIRMATION notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        return _("Payment Confirmation")

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Payment Confirmation - %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, Any]:
        """Get email attachments (PDF ticket and ICS calendar file)."""
        ticket_id = notification.context.get("ticket_id")
        event_id = notification.context.get("event_id")

        if not ticket_id or not event_id:
            return {}

        attachments = {}

        try:
            # Generate ticket PDF
            ticket = Ticket.objects.select_related("event", "user", "tier").get(pk=ticket_id)
            from events.utils import create_ticket_pdf

            pdf_content = create_ticket_pdf(ticket)
            attachments["ticket.pdf"] = {
                "content_base64": base64.b64encode(pdf_content).decode("utf-8"),
                "mimetype": "application/pdf",
            }

            # Generate event ICS calendar file
            event = Event.objects.select_related("city").get(pk=event_id)
            ics_content = event.ics()
            attachments["event.ics"] = {
                "content_base64": base64.b64encode(ics_content).decode("utf-8"),
                "mimetype": "text/calendar",
            }
        except (Ticket.DoesNotExist, Event.DoesNotExist):
            # Log error but don't fail the entire notification
            pass

        return attachments


# Register templates
register_template(NotificationType.TICKET_CREATED, TicketCreatedTemplate())
register_template(NotificationType.TICKET_UPDATED, TicketUpdatedTemplate())
register_template(NotificationType.TICKET_CANCELLED, TicketUpdatedTemplate())
register_template(NotificationType.TICKET_REFUNDED, TicketUpdatedTemplate())
register_template(NotificationType.PAYMENT_CONFIRMATION, PaymentConfirmationTemplate())
