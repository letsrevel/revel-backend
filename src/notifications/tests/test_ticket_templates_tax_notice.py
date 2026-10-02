"""Ticket emails carry the EU "not a tax document" notice (#1085)."""

import typing as t

import pytest
from django.template.loader import render_to_string
from django.utils import translation

from accounts.models import RevelUser
from events.compliance.base import NOT_A_TAX_DOCUMENT_NOTICE
from events.models import Ticket
from notifications.enums import NotificationType
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.ticket_templates import (
    PaymentConfirmationTemplate,
    TicketCreatedTemplate,
    TicketUpdatedTemplate,
)
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db

_HOLDER_EMAILS: list[tuple[NotificationTemplate, NotificationType, dict[str, str]]] = [
    (TicketCreatedTemplate(), NotificationType.TICKET_CREATED, {"ticket_status": "active"}),
    (TicketCreatedTemplate(), NotificationType.TICKET_CREATED, {"ticket_status": "pending"}),
    (
        TicketUpdatedTemplate(),
        NotificationType.TICKET_UPDATED,
        {"old_status": "pending", "new_status": "active", "action": "activated"},
    ),
    (TicketUpdatedTemplate(), NotificationType.TICKET_UPDATED, {"action": "updated"}),
    (PaymentConfirmationTemplate(), NotificationType.PAYMENT_CONFIRMATION, {"payment_amount": "10.00"}),
]


def _render(
    template: NotificationTemplate,
    notification_type: NotificationType,
    extra: dict[str, str],
    user: RevelUser,
    ticket: Ticket,
) -> tuple[str, str]:
    """Render the HTML and text bodies of a ticket email."""
    notification = _create_notification_for_test(
        user=user,
        notification_type=notification_type,
        context={
            "event_name": ticket.event.name,
            "ticket_id": str(ticket.id),
            "event_id": str(ticket.event.id),
            "tier_name": ticket.tier.name,
            **extra,
        },
    )
    html = template.get_email_html_body(notification)
    assert html is not None
    return html, template.get_email_text_body(notification)


@pytest.mark.parametrize(("template", "notification_type", "extra"), _HOLDER_EMAILS)
def test_holder_ticket_emails_carry_the_notice(
    template: NotificationTemplate,
    notification_type: NotificationType,
    extra: dict[str, str],
    ticket_holder: RevelUser,
    active_ticket: Ticket,
) -> None:
    """Every ticket email to the holder says it is not a tax document, in HTML and text."""
    html, text = _render(template, notification_type, extra, ticket_holder, active_ticket)

    assert str(NOT_A_TAX_DOCUMENT_NOTICE) in html
    assert str(NOT_A_TAX_DOCUMENT_NOTICE) in text


@pytest.mark.parametrize(("template", "notification_type", "extra"), _HOLDER_EMAILS)
def test_notice_is_translated(
    template: NotificationTemplate,
    notification_type: NotificationType,
    extra: dict[str, str],
    ticket_holder: RevelUser,
    active_ticket: Ticket,
) -> None:
    """The notice reuses the ticket's catalog entry, so it follows the recipient's language."""
    with translation.override("it"):
        html, text = _render(template, notification_type, extra, ticket_holder, active_ticket)
        italian = str(NOT_A_TAX_DOCUMENT_NOTICE)

    assert italian == "Questo biglietto non è una fattura né uno scontrino fiscale."
    assert italian in html
    assert italian in text


@pytest.mark.parametrize(
    ("template", "notification_type"),
    [
        (TicketCreatedTemplate(), NotificationType.TICKET_CREATED),
        (TicketUpdatedTemplate(), NotificationType.TICKET_UPDATED),
    ],
)
def test_staff_ticket_emails_do_not_carry_the_notice(
    template: NotificationTemplate,
    notification_type: NotificationType,
    ticket_holder: RevelUser,
    active_ticket: Ticket,
) -> None:
    """Staff copies describe someone else's ticket, so the buyer-facing notice stays out."""
    extra = {"ticket_holder_name": "Some Holder", "ticket_holder_email": "holder@example.com"}

    html, text = _render(template, notification_type, extra, ticket_holder, active_ticket)

    assert str(NOT_A_TAX_DOCUMENT_NOTICE) not in html
    assert str(NOT_A_TAX_DOCUMENT_NOTICE) not in text


@pytest.mark.parametrize("partial", ["_tax_notice.html", "_tax_notice.txt"])
def test_notice_partial_runs_no_queries(
    partial: str, django_assert_num_queries: t.Callable[..., t.ContextManager[None]]
) -> None:
    """The notice is static template text: batch sends pay no per-ticket query for it."""
    with django_assert_num_queries(0):
        rendered = render_to_string(f"notifications/email/{partial}")

    assert str(NOT_A_TAX_DOCUMENT_NOTICE) in rendered
