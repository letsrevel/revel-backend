"""Attendance follow-ups for #1278: attendee-only resources, dietary, audience, flags, potluck."""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import DietaryRestriction, FoodItem, RevelUser
from events.models import AdditionalResource, Event, EventRSVP, Ticket, TicketTier
from events.models.mixins import ResourceVisibility
from events.models.potluck import PotluckItem
from events.service.dietary import get_event_dietary_summary
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from notifications.enums import NotificationType
from notifications.service.eligibility import get_eligible_users_for_event_notification

pytestmark = pytest.mark.django_db


def _ticket(event: Event, user: RevelUser, status: Ticket.TicketStatus) -> Ticket:
    tier = TicketTier.objects.get_or_create(event=event, name="General")[0]
    return Ticket.objects.create(event=event, user=user, tier=tier, guest_name="G", status=status)


@pytest.mark.parametrize(
    "status,sees_resource",
    [
        (Ticket.TicketStatus.ACTIVE, True),
        (Ticket.TicketStatus.CHECKED_IN, True),
        (Ticket.TicketStatus.PENDING, False),
    ],
)
def test_attendees_only_resource_visibility_by_ticket_status(
    event: Event, nonmember_user: RevelUser, status: Ticket.TicketStatus, sees_resource: bool
) -> None:
    """Attendee-only resources need attendance, but a PENDING buyer still lists the event itself."""
    resource = AdditionalResource.objects.create(
        organization=event.organization,
        name="R",
        resource_type="text",
        text="x",
        visibility=ResourceVisibility.ATTENDEES_ONLY,
    )
    resource.events.add(event)
    _ticket(event, nonmember_user, status)

    assert AdditionalResource.objects.for_user(nonmember_user).filter(pk=resource.pk).exists() is sees_resource
    assert Event.objects.for_user(nonmember_user).filter(pk=event.pk).exists()


def test_dietary_summary_includes_checked_in_but_not_pending(
    event: Event, organization_owner_user: RevelUser, nonmember_user: RevelUser, member_user: RevelUser
) -> None:
    peanuts = FoodItem.objects.get_or_create(name="Peanuts")[0]
    gluten = FoodItem.objects.get_or_create(name="Gluten")[0]
    for user, food in ((nonmember_user, peanuts), (member_user, gluten)):
        DietaryRestriction.objects.create(
            user=user,
            food_item=food,
            restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
            is_public=True,
        )
    _ticket(event, nonmember_user, Ticket.TicketStatus.CHECKED_IN)
    _ticket(event, member_user, Ticket.TicketStatus.PENDING)

    summary = get_event_dietary_summary(event, organization_owner_user)

    assert {r.food_item for r in summary.restrictions} == {"Peanuts"}


def test_notification_audience_includes_checked_in_and_pending(
    event: Event, nonmember_user: RevelUser, member_user: RevelUser
) -> None:
    """The audience is broader than attendance: CHECKED_IN and PENDING buyers both get updates."""
    _ticket(event, nonmember_user, Ticket.TicketStatus.CHECKED_IN)
    _ticket(event, member_user, Ticket.TicketStatus.PENDING)

    for notification_type in (NotificationType.EVENT_UPDATED, NotificationType.EVENT_CANCELLED):
        ids = {u.id for u in get_eligible_users_for_event_notification(event, notification_type)}
        assert {nonmember_user.id, member_user.id} <= ids, notification_type


def test_preference_change_rebuilds_flags_for_checked_in_holder_at_in_progress_event(
    event: Event, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """In-progress events (started, not ended) are where CHECKED_IN tickets live."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    _ticket(event, nonmember_user, Ticket.TicketStatus.CHECKED_IN)

    with patch("events.service.user_preferences_service.build_attendee_visibility_flags") as task:
        with django_capture_on_commit_callbacks(execute=True):
            trigger_visibility_flags_for_user(nonmember_user.id)

    task.delay.assert_any_call(str(event.id))


def test_ticket_leaving_attending_statuses_releases_potluck_claims(event: Event, nonmember_user: RevelUser) -> None:
    """ACTIVE -> PENDING (e.g. unconfirmed payment) frees the claims; a fresh PENDING ticket is harmless."""
    ticket = _ticket(event, nonmember_user, Ticket.TicketStatus.ACTIVE)
    item = PotluckItem.objects.create(
        event=event, name="Chips", item_type="food", assignee=nonmember_user, created_by=nonmember_user
    )

    ticket.status = Ticket.TicketStatus.PENDING
    ticket.save(update_fields=["status"])

    item.refresh_from_db()
    assert item.assignee is None


def test_creating_pending_ticket_keeps_claims_of_rsvp_yes_user(event: Event, member_user: RevelUser) -> None:
    """An RSVP-YES user may claim without a ticket; a ticket created PENDING must not wipe the claim."""
    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
    item = PotluckItem.objects.create(
        event=event, name="Salsa", item_type="food", assignee=member_user, created_by=member_user
    )

    _ticket(event, member_user, Ticket.TicketStatus.PENDING)

    item.refresh_from_db()
    assert item.assignee == member_user


def test_creating_pending_ticket_keeps_claims_of_org_staff(
    event: Event, organization_staff_user: RevelUser, staff_member: t.Any
) -> None:
    """Org staff may claim without a ticket; a ticket created PENDING must not wipe the claim."""
    item = PotluckItem.objects.create(
        event=event,
        name="Napkins",
        item_type="food",
        assignee=organization_staff_user,
        created_by=organization_staff_user,
    )

    _ticket(event, organization_staff_user, Ticket.TicketStatus.PENDING)

    item.refresh_from_db()
    assert item.assignee == organization_staff_user


def test_address_visibility_cache_is_dropped_when_ticket_is_activated(
    organization: t.Any, nonmember_user: RevelUser
) -> None:
    """A PENDING-time False must not survive activation on the same Event instance."""
    event = Event.objects.create(
        organization=organization,
        name="Cache",
        start=timezone.now(),
        address="1 Street",
        visibility_settings={"address_visibility": ResourceVisibility.ATTENDEES_ONLY},
    )
    ticket = _ticket(event, nonmember_user, Ticket.TicketStatus.PENDING)
    ticket.event = event  # share the loaded instance, as request code does

    assert event.can_user_see_address(nonmember_user) is False

    ticket.status = Ticket.TicketStatus.ACTIVE
    ticket.save()

    assert event.can_user_see_address(nonmember_user) is True


def test_active_to_pending_keeps_claims_of_rsvp_yes_user(event: Event, member_user: RevelUser) -> None:
    """A YES RSVP still permits potluck participation, so losing attendance via the ticket keeps claims."""
    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
    ticket = _ticket(event, member_user, Ticket.TicketStatus.ACTIVE)
    item = PotluckItem.objects.create(
        event=event, name="Dip", item_type="food", assignee=member_user, created_by=member_user
    )

    ticket.status = Ticket.TicketStatus.PENDING
    ticket.save(update_fields=["status"])

    item.refresh_from_db()
    assert item.assignee == member_user
