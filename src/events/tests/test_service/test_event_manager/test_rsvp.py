"""Tests for RSVP functionality and RSVP deadline gate."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
)
from events.service.event_manager import EligibilityService, EventManager, Reasons, UserIsIneligibleError

pytestmark = pytest.mark.django_db


# --- Test Cases for RSVP ---


def test_private_event_rsvp_requires_invitation(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_requires_ticket(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_TICKET
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_with_invitation(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """Test that a user can RSVP with an invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    handler.rsvp(EventRSVP.RsvpStatus.YES)

    rsvp = EventRSVP.objects.filter(event=private_event, user=public_user).first()
    assert rsvp is not None
    assert rsvp.status == EventRSVP.RsvpStatus.YES


# --- Test Cases for RSVP Deadline Gate ---


def test_rsvp_deadline_passed_blocks_access(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline blocks access when deadline has passed."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.RSVP_DEADLINE_PASSED
    assert eligibility.next_step is None


def test_rsvp_deadline_allows_access_before_deadline(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline allows access when deadline has not passed."""
    # Set up event without tickets and with future RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() + timedelta(hours=1)  # 1 hour from now
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_ignored_for_ticket_events(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline is ignored for events that require tickets."""
    # Set up event with tickets and expired RSVP deadline
    public_event.requires_ticket = True
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since RSVP deadline doesn't apply to ticket events
    assert eligibility.allowed is True


def test_rsvp_deadline_waived_by_invitation(public_user: RevelUser, public_event: Event) -> None:
    """Test that invitation can waive RSVP deadline."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    # Create invitation that waives RSVP deadline
    EventInvitation.objects.create(user=public_user, event=public_event, waives_rsvp_deadline=True)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_no_deadline_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that no RSVP deadline allows access."""
    # Set up event without tickets and no RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = None
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for RSVP Status Changes After Requirements Change ---


def test_user_with_yes_rsvp_can_change_to_maybe_after_requirements_change(
    public_user: RevelUser, public_event: Event
) -> None:
    """User who RSVP'd YES can change to MAYBE even if they no longer meet requirements."""
    public_event.requires_ticket = False
    public_event.save()

    # User RSVPs YES while eligible
    handler = EventManager(user=public_user, event=public_event)
    handler.rsvp(EventRSVP.RsvpStatus.YES)

    # Event requirements change - now requires full profile
    public_event.requires_full_profile = True
    public_event.save()

    # Ensure user doesn't have a complete profile
    public_user.profile_picture = None
    public_user.pronouns = ""
    public_user.preferred_name = ""
    public_user.save()

    # Refresh from DB to simulate a new request after requirements changed
    public_event.refresh_from_db()
    public_user.refresh_from_db()

    # User should still be able to change to MAYBE (new handler simulates new request)
    handler = EventManager(user=public_user, event=public_event)
    rsvp = handler.rsvp(EventRSVP.RsvpStatus.MAYBE)

    assert rsvp.status == EventRSVP.RsvpStatus.MAYBE


def test_user_with_yes_rsvp_can_change_to_no_after_requirements_change(
    public_user: RevelUser, public_event: Event
) -> None:
    """User who RSVP'd YES can change to NO even if they no longer meet requirements."""
    public_event.requires_ticket = False
    public_event.save()

    # User RSVPs YES while eligible
    handler = EventManager(user=public_user, event=public_event)
    handler.rsvp(EventRSVP.RsvpStatus.YES)

    # Event requirements change - now requires full profile
    public_event.requires_full_profile = True
    public_event.save()

    # Ensure user doesn't have a complete profile
    public_user.profile_picture = None
    public_user.pronouns = ""
    public_user.preferred_name = ""
    public_user.save()

    # Refresh from DB to simulate a new request after requirements changed
    public_event.refresh_from_db()
    public_user.refresh_from_db()

    # User should still be able to change to NO (new handler simulates new request)
    handler = EventManager(user=public_user, event=public_event)
    rsvp = handler.rsvp(EventRSVP.RsvpStatus.NO)

    assert rsvp.status == EventRSVP.RsvpStatus.NO


def test_user_with_maybe_rsvp_cannot_change_to_yes_after_requirements_change(
    public_user: RevelUser, public_event: Event
) -> None:
    """User who RSVP'd MAYBE cannot change to YES if they no longer meet requirements."""
    public_event.requires_ticket = False
    public_event.save()

    # User RSVPs MAYBE while eligible
    handler = EventManager(user=public_user, event=public_event)
    handler.rsvp(EventRSVP.RsvpStatus.MAYBE)

    # Event requirements change - now requires full profile
    public_event.requires_full_profile = True
    public_event.save()

    # Ensure user doesn't have a complete profile
    public_user.profile_picture = None
    public_user.pronouns = ""
    public_user.preferred_name = ""
    public_user.save()

    # Refresh from DB to simulate a new request after requirements changed
    public_event.refresh_from_db()
    public_user.refresh_from_db()

    # User should NOT be able to change to YES (new handler simulates new request)
    handler = EventManager(user=public_user, event=public_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility
    assert eligibility.reason == Reasons.REQUIRES_FULL_PROFILE

    # RSVP should still be MAYBE
    rsvp = EventRSVP.objects.get(user=public_user, event=public_event)
    assert rsvp.status == EventRSVP.RsvpStatus.MAYBE


# --- Test Cases for RSVP Changes on a Full Event (issue #691) ---


def _configure_rsvp_event(event: Event, *, max_attendees: int = 1) -> None:
    """Turn ``event`` into an RSVP-only, waitlist-open event with the given capacity."""
    event.end = event.start + timedelta(hours=2)
    event.requires_ticket = False
    event.max_attendees = max_attendees
    event.waitlist_open = True
    event.waitlist_time_window = timedelta(hours=24)
    event.save()


def _make_full_rsvp_event(event: Event, factory: RevelUserFactory) -> None:
    """Configure a 1-seat RSVP event and fill it with someone else's YES (at capacity)."""
    _configure_rsvp_event(event)
    EventRSVP.objects.create(event=event, user=factory(), status=EventRSVP.RsvpStatus.YES)


def test_yes_to_no_on_full_event_frees_seat(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A YES holder can downgrade to NO on a full event, freeing their seat (#691)."""
    _configure_rsvp_event(event)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.YES)  # event now full

    with mock.patch("events.service.event_manager.manager.enqueue_waitlist_processing") as enqueue_mock:
        rsvp = EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    assert rsvp.status == EventRSVP.RsvpStatus.NO
    # Freeing a seat must trigger the next waitlist batch.
    enqueue_mock.assert_called_once_with(event.id)


def test_yes_to_maybe_on_full_event(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A YES holder can downgrade to MAYBE on a full event (#691)."""
    _configure_rsvp_event(event)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.YES)  # event now full

    rsvp = EventManager(me, event).rsvp(EventRSVP.RsvpStatus.MAYBE)

    assert rsvp.status == EventRSVP.RsvpStatus.MAYBE


def test_maybe_to_no_on_full_event(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A MAYBE holder can change to NO on a full event — neither answer consumes a seat (#691)."""
    _make_full_rsvp_event(event, revel_user_factory)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.MAYBE)

    rsvp = EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    assert rsvp.status == EventRSVP.RsvpStatus.NO


def test_new_no_rsvp_on_full_event_allowed(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A new NO RSVP (e.g. declining) is not seat-consuming and must pass on a full event (#691)."""
    _make_full_rsvp_event(event, revel_user_factory)
    me = revel_user_factory()

    rsvp = EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    assert rsvp.status == EventRSVP.RsvpStatus.NO


def test_new_yes_rsvp_on_full_event_still_rejected(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Regression guard: a brand-new YES on a full event is still capacity-gated (#691)."""
    _make_full_rsvp_event(event, revel_user_factory)
    me = revel_user_factory()

    with pytest.raises(UserIsIneligibleError) as exc_info:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.YES)

    assert exc_info.value.eligibility.reason_code == Reasons.EVENT_IS_FULL.code
    assert not EventRSVP.objects.filter(event=event, user=me).exists()


def test_maybe_to_yes_on_full_event_still_rejected(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Regression guard: MAYBE -> YES claims a seat, so it is still capacity-gated on a full event (#691)."""
    _make_full_rsvp_event(event, revel_user_factory)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.MAYBE)

    with pytest.raises(UserIsIneligibleError) as exc_info:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.YES)

    assert exc_info.value.eligibility.reason_code == Reasons.EVENT_IS_FULL.code
    # The RSVP must remain MAYBE — the failed upgrade must not have changed it.
    assert EventRSVP.objects.get(event=event, user=me).status == EventRSVP.RsvpStatus.MAYBE
