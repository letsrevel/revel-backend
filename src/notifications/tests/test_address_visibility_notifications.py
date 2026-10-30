"""Notification producers must apply the per-recipient address / cancellation-reason policy."""

import base64
import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, EventInvitation, EventRSVP, EventWaitList, PendingEventInvitation, Ticket, TicketTier
from events.utils.visibility_settings import ResourceVisibility
from notifications.enums import NotificationType
from notifications.service.templates.ticket_templates import _build_ticket_attachments
from notifications.signals import event as event_signals
from notifications.signals import ticket as ticket_signals
from notifications.signals import waitlist as waitlist_signals
from notifications.tasks import send_pending_invitation_email

pytestmark = pytest.mark.django_db

ADDRESS = "1 Hidden Street"


def _restrict(event: Event, visibility: ResourceVisibility) -> Event:
    event.address = ADDRESS
    event.visibility_settings = {"address_visibility": visibility.value}
    event.save()
    return event


def _contexts(send_mock: MagicMock, user: RevelUser | None = None) -> list[dict[str, t.Any]]:
    return [c.kwargs["context"] for c in send_mock.call_args_list if user is None or c.kwargs["user"] == user]


def test_event_cancelled_reason_only_for_attendees(
    public_event: Event,
    nonmember_user: RevelUser,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Invited-only users do not get the cancellation reason; confirmed RSVPs do."""
    public_event.cancellation_reason = "Venue flooded"
    EventInvitation.objects.create(event=public_event, user=nonmember_user)
    EventRSVP.objects.create(event=public_event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    eligible = MagicMock()
    eligible.__iter__.return_value = iter([nonmember_user, member_user])
    eligible.count.return_value = 2

    with (
        patch.object(event_signals, "get_eligible_users_for_event_notification", return_value=eligible),
        patch("notifications.signals.event.notification_requested.send") as send_mock,
        django_capture_on_commit_callbacks(execute=True),
    ):
        event_signals._handle_event_cancelled(Event, public_event)

    assert "cancellation_reason" not in _contexts(send_mock, nonmember_user)[0]
    assert _contexts(send_mock, member_user)[0]["cancellation_reason"] == "Venue flooded"


def test_follower_notification_hides_restricted_address(
    public_event: Event, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A plain follower of the org does not receive an attendees-only address."""
    public_event.status = Event.EventStatus.DRAFT
    _restrict(public_event, ResourceVisibility.ATTENDEES_ONLY)
    follower = [(nonmember_user, NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG)]

    with (
        patch("events.signals.get_followers_for_new_event_notification", return_value=follower),
        patch("events.signals.notification_requested.send") as send_mock,
        django_capture_on_commit_callbacks(execute=True),
    ):
        public_event.status = Event.EventStatus.OPEN
        public_event.save()

    contexts = _contexts(send_mock, nonmember_user)
    assert contexts
    assert contexts[0]["event_location"] == ""


@pytest.mark.parametrize(
    ("visibility", "shown"),
    [(ResourceVisibility.ATTENDEES_ONLY, False), (ResourceVisibility.PRIVATE, True)],
)
def test_invitation_notification_respects_address_visibility(
    public_event: Event,
    nonmember_user: RevelUser,
    visibility: ResourceVisibility,
    shown: bool,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Invitation alone does not entitle an invitee to an attendees-only address."""
    _restrict(public_event, visibility)

    with (
        patch("notifications.signals.invitation.notification_requested.send") as send_mock,
        django_capture_on_commit_callbacks(execute=True),
    ):
        EventInvitation.objects.create(event=public_event, user=nonmember_user)

    contexts = _contexts(send_mock, nonmember_user)
    assert contexts
    assert (contexts[0]["event_location"] == ADDRESS) is shown
    if not shown:
        assert contexts[0]["event_location"] == ""


def test_waitlist_spot_notification_hides_restricted_address(public_event: Event, nonmember_user: RevelUser) -> None:
    """A waitlisted non-attendee does not receive an attendees-only address."""
    _restrict(public_event, ResourceVisibility.ATTENDEES_ONLY)
    EventWaitList.objects.create(event=public_event, user=nonmember_user)

    with patch("notifications.signals.waitlist.notification_requested.send") as send_mock:
        waitlist_signals._notify_waitlist(public_event, 1)

    assert _contexts(send_mock)[0]["event_location"] == ""


@pytest.mark.parametrize(
    ("visibility", "shown"),
    [(ResourceVisibility.ATTENDEES_ONLY, False), (ResourceVisibility.PUBLIC, True)],
)
def test_pending_invitation_email_address_policy(
    public_event: Event, visibility: ResourceVisibility, shown: bool, settings: t.Any
) -> None:
    """Pending (account-less) invitees are treated like anonymous users."""
    settings.DEFAULT_FROM_EMAIL = "Let's Revel <revel@letsrevel.io>"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    _restrict(public_event, visibility)
    pending = PendingEventInvitation.objects.create(event=public_event, email="invitee@example.com")

    send_pending_invitation_email.apply(args=(str(pending.id),))

    msg = mail.outbox[-1]
    html = str(t.cast(EmailMultiAlternatives, msg).alternatives[0][0])
    assert (ADDRESS in msg.body) is shown
    assert (ADDRESS in html) is shown


@pytest.fixture
def buyer_ticket(ticket_event: Event, ticket_tier: TicketTier, nonmember_user: RevelUser) -> Ticket:
    """A ticket held by a plain buyer (the fixture ``active_ticket`` is held by the org owner)."""
    return Ticket.objects.create(
        guest_name="Buyer",
        user=nonmember_user,
        event=ticket_event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


def test_ticket_holder_context_hides_restricted_address_but_staff_keeps_it(
    ticket_event: Event, buyer_ticket: Ticket
) -> None:
    """A holder outside the address audience gets no address; staff context is unrestricted."""
    _restrict(ticket_event, ResourceVisibility.MEMBERS_ONLY)
    ticket = Ticket.objects.get(pk=buyer_ticket.pk)

    holder_context = ticket_signals._build_ticket_created_context(ticket)
    staff_context = ticket_signals._staff_location_context(ticket.event)

    assert holder_context["event_location"] == ""
    assert "address_url" not in holder_context
    assert staff_context["event_location"] == ADDRESS


def test_ticket_ics_and_pdf_omit_restricted_address(ticket_event: Event, buyer_ticket: Ticket) -> None:
    """Ticket attachments for a holder outside the address audience carry no address."""
    from events.utils import create_ticket_pdf

    _restrict(ticket_event, ResourceVisibility.MEMBERS_ONLY)

    attachments = _build_ticket_attachments(
        str(buyer_ticket.id), str(ticket_event.id), include_pdf=False, include_ics=True, include_pkpass=False
    )
    ics = base64.b64decode(attachments["event.ics"]["content_base64"]).decode()
    assert ADDRESS not in ics

    ticket = Ticket.objects.get(pk=buyer_ticket.pk)
    with patch("events.utils.render_to_string", return_value="<html></html>") as render:
        create_ticket_pdf(ticket)
    assert render.call_args.kwargs["context"]["address"] == ""


def test_ticket_ics_includes_address_when_visible(ticket_event: Event, active_ticket: Ticket) -> None:
    """Public addresses still appear in the calendar attachment."""
    _restrict(ticket_event, ResourceVisibility.PUBLIC)

    attachments = _build_ticket_attachments(
        str(active_ticket.id), str(ticket_event.id), include_pdf=False, include_ics=True, include_pkpass=False
    )

    assert ADDRESS in base64.b64decode(attachments["event.ics"]["content_base64"]).decode()
