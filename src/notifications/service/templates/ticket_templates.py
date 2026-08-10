"""Templates for ticket-related notifications."""

import base64
import time
import typing as t
from datetime import timedelta

import structlog
from django.conf import settings
from django.utils.translation import gettext as _

from events.models import Event, Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template

logger = structlog.get_logger(__name__)


# --- Attachment Generation Helpers ---


class AttachmentResult(t.NamedTuple):
    """Result of generating a ticket attachment (PDF or pkpass)."""

    attachment: dict[str, str]
    raw_bytes: bytes


def _generate_pdf_attachment(ticket: Ticket) -> AttachmentResult | None:
    """Generate PDF attachment for a ticket.

    Args:
        ticket: The ticket to generate PDF for.

    Returns:
        AttachmentResult or None on error.
    """
    try:
        from events.utils import create_ticket_pdf

        pdf_content = create_ticket_pdf(ticket)
        attachment = {
            "content_base64": base64.b64encode(pdf_content).decode("utf-8"),
            "mimetype": "application/pdf",
        }
        return AttachmentResult(attachment, pdf_content)
    except Exception:
        logger.exception("failed_to_generate_pdf", ticket_id=str(ticket.id))
        return None


def _generate_ics_attachment(event: Event) -> dict[str, t.Any] | None:
    """Generate ICS calendar attachment for an event.

    Args:
        event: The event to generate ICS for.

    Returns:
        Attachment dict with content_base64 and mimetype, or None on error.
    """
    try:
        ics_content = event.ics()
        return {
            "content_base64": base64.b64encode(ics_content).decode("utf-8"),
            "mimetype": "text/calendar",
        }
    except Exception:
        logger.exception("failed_to_generate_ics", event_id=str(event.id))
        return None


def _generate_pkpass_attachment(ticket: Ticket) -> AttachmentResult | None:
    """Generate Apple Wallet pkpass attachment for a ticket.

    Uses the cached ApplePassGenerator from ticket_file_service to avoid
    reloading certificates from disk on every call.

    Args:
        ticket: The ticket to generate pkpass for.

    Returns:
        AttachmentResult or None if not available.
    """
    if not ticket.apple_pass_available:
        return None

    # Never attach a scannable pass for a dead ticket. Only CANCELLED is excluded
    # (not the ACTIVE/PENDING allowlist the Google link uses) because checked-in
    # emails intentionally still attach the pass.
    if ticket.status == Ticket.TicketStatus.CANCELLED:
        return None

    try:
        from events.service.ticket_file_service import get_apple_pass_generator

        generator = get_apple_pass_generator()
        pkpass_content = generator.generate_pass(ticket)
        attachment = {
            "content_base64": base64.b64encode(pkpass_content).decode("utf-8"),
            "mimetype": "application/vnd.apple.pkpass",
        }
        return AttachmentResult(attachment, pkpass_content)
    except Exception:
        logger.exception("failed_to_generate_pkpass", ticket_id=str(ticket.id))
        return None


def _google_wallet_save_url(ticket: Ticket) -> str | None:
    """Build a Google Wallet save link for a ticket email, or None.

    Never raises: email rendering must not fail because a wallet link could
    not be generated (mirrors the pkpass attachment posture).
    """
    if not ticket.google_pass_available:
        return None
    try:
        from wallet.google import service as google_wallet_service

        return google_wallet_service.ticket_save_url(ticket)
    except Exception:
        logger.exception("failed_to_generate_google_wallet_link", ticket_id=str(ticket.id))
        return None


# How long past event end an emailed signed pkpass link keeps working (user decision).
SIGNED_LINK_GRACE_PERIOD = timedelta(weeks=1)


def _apple_wallet_signed_url(ticket: Ticket) -> str | None:
    """Build a signed, auth-free Apple Wallet download link for a ticket email.

    The link expires one week after the event ends, so an emailed badge never
    outlives the ticket's usefulness by much. Never raises: email rendering
    must not fail because a wallet link could not be built.
    """
    if not ticket.apple_pass_available:
        return None
    try:
        from django.urls import reverse

        from common.signing import generate_signature

        expires = int((ticket.event.end + SIGNED_LINK_GRACE_PERIOD).timestamp())
        if expires <= time.time():
            return None
        path = reverse("api:ticket_apple_wallet_signed", kwargs={"ticket_id": ticket.id})
        sig = generate_signature(path, expires)
        return f"{settings.BASE_URL.rstrip('/')}{path}?exp={expires}&sig={sig}"
    except Exception:
        logger.exception("failed_to_generate_apple_wallet_link", ticket_id=str(ticket.id))
        return None


def _add_wallet_context(base_context: dict[str, t.Any], notification: Notification) -> dict[str, t.Any]:
    """Add wallet badge URLs to a ticket email template context.

    Sets ``apple_wallet_signed_url`` and ``google_wallet_save_url`` when the
    respective rail is configured. Gated the same way as the pkpass
    attachment: the ``include_pkpass`` context flag plus server-side
    availability and ticket status.
    """
    from events.utils import apple_wallet_configured, google_wallet_configured

    if not (apple_wallet_configured() or google_wallet_configured()):
        return base_context
    if not notification.context.get("include_pkpass", True):
        return base_context
    ticket_id = notification.context.get("ticket_id")
    if not ticket_id:
        return base_context
    ticket = _load_ticket(ticket_id)
    if ticket is None or ticket.status not in (Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING):
        # Mirrors TicketWalletController.get_queryset()'s status filter: TICKET_CANCELLED and
        # TICKET_REFUNDED notifications reuse TicketUpdatedTemplate with include_pkpass
        # defaulting True, so a dead ticket must not get a save link.
        return base_context
    if url := _google_wallet_save_url(ticket):
        base_context["context"]["google_wallet_save_url"] = url
    if apple_url := _apple_wallet_signed_url(ticket):
        base_context["context"]["apple_wallet_signed_url"] = apple_url
    return base_context


def _load_ticket(ticket_id: str) -> Ticket | None:
    """Load a ticket by ID with related objects prefetched.

    Args:
        ticket_id: UUID of the ticket.

    Returns:
        Ticket instance or None if not found.
    """
    try:
        # TicketManager already selects event and event__organization by default
        return Ticket.objects.full().get(pk=ticket_id)
    except Ticket.DoesNotExist:
        logger.warning("ticket_not_found_for_attachment", ticket_id=str(ticket_id))
        return None


def _load_event(event_id: str) -> Event | None:
    """Load an event by ID with related objects prefetched.

    Args:
        event_id: UUID of the event.

    Returns:
        Event instance or None if not found.
    """
    try:
        return Event.objects.select_related("city").get(pk=event_id)
    except Event.DoesNotExist:
        logger.warning("event_not_found_for_attachment", event_id=str(event_id))
        return None


def _build_ticket_attachments(
    ticket_id: str | None,
    event_id: str | None,
    include_pdf: bool = True,
    include_ics: bool = True,
    include_pkpass: bool = True,
) -> dict[str, t.Any]:
    """Build attachments dict for ticket-related notifications.

    Args:
        ticket_id: UUID of the ticket.
        event_id: UUID of the event.
        include_pdf: Whether to include PDF attachment.
        include_ics: Whether to include ICS calendar attachment.
        include_pkpass: Whether to include Apple Wallet pkpass attachment.

    Returns:
        Dict of filename -> attachment data.
    """
    if not ticket_id or not event_id:
        return {}

    attachments: dict[str, t.Any] = {}

    # Load ticket if needed for PDF or pkpass
    ticket = _load_ticket(ticket_id) if include_pdf or include_pkpass else None

    # Load event if needed for ICS
    event = _load_event(event_id) if include_ics else None

    # Generate attachments — helpers return (attachment_dict, raw_bytes) tuples
    # so raw bytes are available for caching without a base64 round-trip.
    pdf_bytes: bytes | None = None
    pkpass_bytes: bytes | None = None

    if include_pdf and ticket:
        pdf_result = _generate_pdf_attachment(ticket)
        if pdf_result is not None:
            attachments["ticket.pdf"], pdf_bytes = pdf_result

    if include_ics and event:
        if ics := _generate_ics_attachment(event):
            attachments["event.ics"] = ics

    if include_pkpass and ticket:
        pkpass_result = _generate_pkpass_attachment(ticket)
        if pkpass_result is not None:
            attachments["ticket.pkpass"], pkpass_bytes = pkpass_result

    # Side effect: persist generated files on the ticket so subsequent
    # download requests can serve them from cache via signed URLs.
    if ticket and (pdf_bytes is not None or pkpass_bytes is not None):
        try:
            from events.service import ticket_file_service

            ticket_file_service.cache_files(ticket, pdf_bytes=pdf_bytes, pkpass_bytes=pkpass_bytes)
        except Exception:
            logger.warning("failed_to_cache_ticket_files", ticket_id=str(ticket.id), exc_info=True)

    return attachments


# --- Template Classes ---


class TicketCreatedTemplate(NotificationTemplate):
    """Template for TICKET_CREATED notification."""

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Enrich the base context with wallet badge links."""
        return _add_wallet_context(super()._get_template_context(notification), notification)

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        ticket_status = notification.context.get("ticket_status", "")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("New Ticket: %(holder)s - %(event)s") % {"holder": ticket_holder_name, "event": event_name}
        # Notification to ticket holder
        if ticket_status == "pending":
            return _("Ticket Pending for %(event)s") % {"event": event_name}
        return _("Ticket Confirmed for %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        ticket_status = notification.context.get("ticket_status", "")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("New Ticket: %(holder)s - %(event)s") % {"holder": ticket_holder_name, "event": event_name}
        # Notification to ticket holder
        if ticket_status == "pending":
            return _("Ticket Pending - %(event)s") % {"event": event_name}
        return _("Ticket Confirmed - %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, t.Any]:
        """Get attachments (ticket PDF, ICS, and optionally pkpass).

        Conditionally includes PDF based on ticket status and payment method.
        Always includes ICS calendar file.
        Includes pkpass if Apple Wallet is configured.
        """
        return _build_ticket_attachments(
            ticket_id=notification.context.get("ticket_id"),
            event_id=notification.context.get("event_id"),
            include_pdf=notification.context.get("include_pdf", True),
            include_ics=notification.context.get("include_ics", True),
            include_pkpass=notification.context.get("include_pkpass", True),
        )


class TicketUpdatedTemplate(NotificationTemplate):
    """Template for TICKET_UPDATED notification."""

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Enrich the base context with wallet badge links."""
        return _add_wallet_context(super()._get_template_context(notification), notification)

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        action = notification.context.get("action", "updated")
        old_status = notification.context.get("old_status", "")
        new_status = notification.context.get("new_status", "")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("Ticket %(action)s: %(holder)s - %(event)s") % {
                "action": action.capitalize(),
                "holder": ticket_holder_name,
                "event": event_name,
            }
        # Notification to ticket holder
        if old_status == "pending" and new_status == "active":
            return _("Ticket Confirmed for %(event)s") % {"event": event_name}
        return _("Ticket Update for %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        ticket_holder_name = notification.context.get("ticket_holder_name")
        action = notification.context.get("action", "updated")
        old_status = notification.context.get("old_status", "")
        new_status = notification.context.get("new_status", "")

        if ticket_holder_name:
            # Notification to staff/owners
            return _("Ticket %(action)s: %(holder)s - %(event)s") % {
                "action": action.capitalize(),
                "holder": ticket_holder_name,
                "event": event_name,
            }
        # Notification to ticket holder
        if old_status == "pending" and new_status == "active":
            return _("Ticket Confirmed - %(event)s") % {"event": event_name}
        return _("Ticket Update - %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, t.Any]:
        """Get attachments (ticket PDF, ICS, and optionally pkpass).

        For ticket activations, include PDF, ICS, and pkpass.
        For cancellations/refunds, no attachments are included.
        """
        include_pdf = notification.context.get("include_pdf", True)
        include_ics = notification.context.get("include_ics", True)
        include_pkpass = notification.context.get("include_pkpass", True)

        # No attachments for cancellations/refunds (all flags will be False)
        if not include_pdf and not include_ics and not include_pkpass:
            return {}

        return _build_ticket_attachments(
            ticket_id=notification.context.get("ticket_id"),
            event_id=notification.context.get("event_id"),
            include_pdf=include_pdf,
            include_ics=include_ics,
            include_pkpass=include_pkpass,
        )


class PaymentConfirmationTemplate(NotificationTemplate):
    """Template for PAYMENT_CONFIRMATION notification."""

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Enrich the base context with wallet badge links."""
        return _add_wallet_context(super()._get_template_context(notification), notification)

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        return _("Payment Confirmation")

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Payment Confirmation - %(event)s") % {"event": event_name}

    def get_email_attachments(self, notification: Notification) -> dict[str, t.Any]:
        """Get email attachments (PDF ticket, ICS calendar, and optionally pkpass)."""
        return _build_ticket_attachments(
            ticket_id=notification.context.get("ticket_id"),
            event_id=notification.context.get("event_id"),
            include_pdf=True,
            include_ics=True,
            include_pkpass=True,
        )


class TicketCheckedInTemplate(NotificationTemplate):
    """Template for TICKET_CHECKED_IN notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("Checked in for %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Checked in - %(event)s") % {"event": event_name}


class RefundUnmatchedTemplate(NotificationTemplate):
    """Template for REFUND_UNMATCHED notification (to org staff)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        return _("Refund could not be matched to a ticket")

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Action needed: unmatched refund - %(org)s") % {"org": org_name}


class EventRefundSummaryTemplate(NotificationTemplate):
    """Template for EVENT_REFUND_SUMMARY notification (to org staff)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("Refund sweep finished - %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Refund sweep finished - %(event)s") % {"event": event_name}


# Register templates
register_template(NotificationType.TICKET_CREATED, TicketCreatedTemplate())
register_template(NotificationType.TICKET_UPDATED, TicketUpdatedTemplate())
register_template(NotificationType.TICKET_CANCELLED, TicketUpdatedTemplate())
register_template(NotificationType.TICKET_REFUNDED, TicketUpdatedTemplate())
register_template(NotificationType.TICKET_CHECKED_IN, TicketCheckedInTemplate())
register_template(NotificationType.PAYMENT_CONFIRMATION, PaymentConfirmationTemplate())
register_template(NotificationType.REFUND_UNMATCHED, RefundUnmatchedTemplate())
register_template(NotificationType.EVENT_REFUND_SUMMARY, EventRefundSummaryTemplate())
