# src/events/management/commands/bootstrap_helpers/relationships.py
"""User relationship creation for bootstrap process (invitations, tickets, RSVPs, waitlists)."""

import datetime
from datetime import timedelta
from decimal import Decimal

import structlog
from django.utils import timezone

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_user_relationships(state: BootstrapState) -> None:
    """Create comprehensive user relationships: invitations, tickets, RSVPs, waitlists."""
    logger.info("Creating user relationships...")

    now = timezone.now()

    _create_invitations(state, now)
    _create_tickets(state, now)
    _create_rsvps(state)
    _create_waitlists(state)

    logger.info("Created user relationships (invitations, tickets, RSVPs, waitlists)")


def _create_invitations(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create event invitations."""
    wine_tier = events_models.TicketTier.objects.get(event=state.events["wine_tasting"], name="Exclusive Seating")

    events_models.EventInvitation.objects.create(
        event=state.events["wine_tasting"],
        user=state.users["multi_org_user"],
        waives_questionnaire=True,
        waives_purchase=False,
        tier=wine_tier,
        custom_message="You're invited to our exclusive wine tasting dinner!",
    )

    events_models.EventInvitation.objects.create(
        event=state.events["wine_tasting"],
        user=state.users["attendee_1"],
        waives_questionnaire=True,
        waives_purchase=True,  # Complimentary
        tier=wine_tier,
        custom_message="As a valued member, please join us as our guest!",
    )

    # Pending invitation (email not yet registered)
    events_models.PendingEventInvitation.objects.create(
        event=state.events["wine_tasting"],
        email="vip.guest@example.com",
        waives_questionnaire=True,
        waives_purchase=False,
        tier=wine_tier,
        custom_message="We'd love for you to join our exclusive wine tasting event!",
    )


def _create_tickets(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create tickets for various events."""
    _create_summer_festival_tickets(state, now)
    _create_past_event_tickets(state, now)
    _create_wellness_retreat_tickets(state)
    _create_tech_conference_tickets(state)
    _create_seated_concert_tickets(state)


def _create_summer_festival_tickets(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Summer Festival tickets."""
    festival_early_bird = events_models.TicketTier.objects.get(
        event=state.events["summer_festival"], name="Early Bird General Admission"
    )
    festival_general = events_models.TicketTier.objects.get(
        event=state.events["summer_festival"], name="General Admission"
    )
    festival_vip = events_models.TicketTier.objects.get(event=state.events["summer_festival"], name="VIP Experience")

    # Active tickets
    for user_key in ["attendee_1", "attendee_2", "attendee_3", "multi_org_user"]:
        user = state.users[user_key]
        events_models.Ticket.objects.create(
            guest_name=user.get_display_name(),
            event=state.events["summer_festival"],
            user=user,
            tier=festival_early_bird,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

    # VIP tickets
    events_models.Ticket.objects.create(
        guest_name=state.users["org_alpha_owner"].get_display_name(),
        event=state.events["summer_festival"],
        user=state.users["org_alpha_owner"],
        tier=festival_vip,
        status=events_models.Ticket.TicketStatus.ACTIVE,
    )

    # Pending ticket (payment not completed)
    pending_ticket = events_models.Ticket.objects.create(
        guest_name=state.users["attendee_4"].get_display_name(),
        event=state.events["summer_festival"],
        user=state.users["attendee_4"],
        tier=festival_general,
        status=events_models.Ticket.TicketStatus.PENDING,
    )

    # Payment for pending ticket
    events_models.Payment.objects.create(
        ticket=pending_ticket,
        user=state.users["attendee_4"],
        stripe_session_id=f"cs_test_{state.fake.uuid4()}",
        status=events_models.Payment.PaymentStatus.PENDING,
        amount=Decimal("65.00"),
        platform_fee=Decimal("6.50"),
        currency="USD",
        expires_at=now + timedelta(minutes=30),
    )

    # Cancelled ticket
    cancelled_ticket = events_models.Ticket.objects.create(
        guest_name=state.users["pending_user"].get_display_name(),
        event=state.events["summer_festival"],
        user=state.users["pending_user"],
        tier=festival_early_bird,
        status=events_models.Ticket.TicketStatus.CANCELLED,
    )

    events_models.Payment.objects.create(
        ticket=cancelled_ticket,
        user=state.users["pending_user"],
        stripe_session_id=f"cs_test_{state.fake.uuid4()}",
        status=events_models.Payment.PaymentStatus.REFUNDED,
        amount=Decimal("45.00"),
        platform_fee=Decimal("4.50"),
        currency="USD",
        expires_at=now - timedelta(days=5),
    )


def _create_past_event_tickets(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create past event checked-in tickets."""
    past_tier = events_models.TicketTier.objects.get(event=state.events["past_event"], name="Gala Ticket")

    checked_in_ticket = events_models.Ticket.objects.create(
        guest_name=state.users["attendee_1"].get_display_name(),
        event=state.events["past_event"],
        user=state.users["attendee_1"],
        tier=past_tier,
        status=events_models.Ticket.TicketStatus.CHECKED_IN,
        checked_in_at=now - timedelta(days=89, hours=2),
        checked_in_by=state.users["org_alpha_staff"],
    )

    # Payment for past event
    events_models.Payment.objects.create(
        ticket=checked_in_ticket,
        user=state.users["attendee_1"],
        stripe_session_id=f"cs_test_{state.fake.uuid4()}",
        status=events_models.Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("250.00"),
        platform_fee=Decimal("25.00"),
        currency="USD",
        expires_at=now - timedelta(days=120),
    )


def _create_wellness_retreat_tickets(state: BootstrapState) -> None:
    """Create Wellness Retreat tickets."""
    wellness_tier = events_models.TicketTier.objects.get(event=state.events["wellness_retreat"], name="Shared Room")

    events_models.Ticket.objects.create(
        guest_name=state.users["attendee_2"].get_display_name(),
        event=state.events["wellness_retreat"],
        user=state.users["attendee_2"],
        tier=wellness_tier,
        status=events_models.Ticket.TicketStatus.ACTIVE,
    )


def _create_tech_conference_tickets(state: BootstrapState) -> None:
    """Create Tech Conference member discount tickets."""
    conf_member_tier = events_models.TicketTier.objects.get(
        event=state.events["tech_conference"], name="Member Discount"
    )

    events_models.Ticket.objects.create(
        guest_name=state.users["org_beta_member"].get_display_name(),
        event=state.events["tech_conference"],
        user=state.users["org_beta_member"],
        tier=conf_member_tier,
        status=events_models.Ticket.TicketStatus.ACTIVE,
    )


def _create_seated_concert_tickets(state: BootstrapState) -> None:
    """Create Classical Music Evening tickets (offline payment demo)."""
    standing_tier = events_models.TicketTier.objects.get(event=state.events["seated_concert"], name="Standing Room")

    # Tickets with confirmed payment (ACTIVE)
    for user_key in ["attendee_3", "org_alpha_member", "multi_org_user"]:
        events_models.Ticket.objects.create(
            guest_name=state.users[user_key].get_display_name(),
            event=state.events["seated_concert"],
            user=state.users[user_key],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

    # Tickets awaiting payment confirmation (PENDING)
    for user_key in ["attendee_1", "attendee_2", "attendee_4", "pending_user", "invited_user"]:
        events_models.Ticket.objects.create(
            guest_name=state.users[user_key].get_display_name(),
            event=state.events["seated_concert"],
            user=state.users[user_key],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )


def _create_rsvps(state: BootstrapState) -> None:
    """Create RSVPs for events without tickets."""
    # Spring potluck RSVPs
    rsvp_users_yes = ["attendee_1", "attendee_2", "attendee_3", "attendee_4", "multi_org_user", "org_alpha_member"]
    for user_key in rsvp_users_yes:
        events_models.EventRSVP.objects.create(
            event=state.events["spring_potluck"],
            user=state.users[user_key],
            status=events_models.EventRSVP.RsvpStatus.YES,
        )

    # Maybe RSVPs
    events_models.EventRSVP.objects.create(
        event=state.events["spring_potluck"],
        user=state.users["org_alpha_staff"],
        status=events_models.EventRSVP.RsvpStatus.MAYBE,
    )

    # No RSVP
    events_models.EventRSVP.objects.create(
        event=state.events["spring_potluck"],
        user=state.users["pending_user"],
        status=events_models.EventRSVP.RsvpStatus.NO,
    )

    # Tech workshop RSVPs (members only)
    for user_key in ["org_beta_member", "org_beta_staff", "multi_org_user"]:
        events_models.EventRSVP.objects.create(
            event=state.events["tech_workshop"],
            user=state.users[user_key],
            status=events_models.EventRSVP.RsvpStatus.YES,
        )

    # Tech talk RSVPs
    events_models.EventRSVP.objects.create(
        event=state.events["tech_talk_may"],
        user=state.users["org_beta_member"],
        status=events_models.EventRSVP.RsvpStatus.YES,
    )

    # Networking event RSVPs
    for user_key in ["org_beta_member", "org_beta_staff", "multi_org_user", "attendee_1"]:
        events_models.EventRSVP.objects.create(
            event=state.events["networking_event"],
            user=state.users[user_key],
            status=events_models.EventRSVP.RsvpStatus.YES,
        )

    # Art opening RSVPs
    for user_key in ["attendee_2", "attendee_3", "org_alpha_member"]:
        events_models.EventRSVP.objects.create(
            event=state.events["art_opening"],
            user=state.users[user_key],
            status=events_models.EventRSVP.RsvpStatus.YES,
        )


def _create_waitlists(state: BootstrapState) -> None:
    """Create waitlist entries."""
    # ML Workshop waitlist (sold out)
    for user_key in ["attendee_3", "attendee_4", "invited_user"]:
        events_models.EventWaitList.objects.create(
            event=state.events["sold_out_workshop"],
            user=state.users[user_key],
        )

    # Summer festival waitlist (near capacity)
    events_models.EventWaitList.objects.create(
        event=state.events["summer_festival"],
        user=state.users["invited_user"],
    )
