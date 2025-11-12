"""Templates for ticket-related notifications."""

import base64
from typing import Any

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from events.models import Event, Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class TicketCreatedTemplate(NotificationTemplate):
    """Template for TICKET_CREATED notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        event_name = notification.context.get("event_name", "")
        return _("Ticket Confirmation for %(event)s") % {"event": event_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        return _("Your ticket for **%(event)s** has been confirmed! Your ticket reference is: %(reference)s") % {
            "event": ctx.get("event_name", ""),
            "reference": ctx.get("ticket_reference", ""),
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Your ticket for %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/ticket_created.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/ticket_created.html", {"user": notification.user, "context": notification.context}
        )

    def get_attachments(self, notification: Notification) -> dict[str, Any]:
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

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        event_name = notification.context.get("event_name", "")
        return _("Ticket Update for %(event)s") % {"event": event_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        action = ctx.get("action", "updated")
        return _("Your ticket for **%(event)s** has been %(action)s.") % {
            "event": ctx.get("event_name", ""),
            "action": action,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Ticket Update - %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/ticket_updated.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/ticket_updated.html", {"user": notification.user, "context": notification.context}
        )

    def get_attachments(self, notification: Notification) -> dict[str, Any]:
        """Get attachments (ticket PDF + ICS).

        For ticket updates (especially activations), always include both PDF and ICS.
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


class PaymentConfirmationTemplate(NotificationTemplate):
    """Template for PAYMENT_CONFIRMATION notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        return _("Payment Confirmation")

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        return _("Your payment of %(amount)s for %(event)s has been confirmed.") % {
            "amount": ctx.get("amount", ""),
            "event": ctx.get("event_name", ""),
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Payment Confirmation - %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/payment_confirmation.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/payment_confirmation.html",
            {"user": notification.user, "context": notification.context},
        )

    def get_attachments(self, notification: Notification) -> dict[str, Any]:
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
register_template(NotificationType.PAYMENT_CONFIRMATION, PaymentConfirmationTemplate())
