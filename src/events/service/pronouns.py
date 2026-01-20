"""Pronoun distribution aggregation for events."""

from django.db.models import Count, Q

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.schema import EventPronounDistributionSchema, PronounCountSchema


def get_event_pronoun_distribution(event: Event) -> EventPronounDistributionSchema:
    """Get aggregated pronoun distribution for event attendees.

    Attendees are defined as:
    - Users with RSVP status YES
    - Users with active tickets (for online payment)
    - Users with any ticket status for offline/at_the_door/free payment methods

    Args:
        event: The event to get pronoun distribution for

    Returns:
        EventPronounDistributionSchema with distribution and totals
    """
    # Build filter for tickets that count as attendance:
    # - Online payment: must be ACTIVE status
    # - Non-online payment (offline/at_the_door/free): any non-cancelled status
    non_online_methods = [
        TicketTier.PaymentMethod.OFFLINE,
        TicketTier.PaymentMethod.AT_THE_DOOR,
        TicketTier.PaymentMethod.FREE,
    ]

    valid_non_online_statuses = [
        Ticket.TicketStatus.ACTIVE,
        Ticket.TicketStatus.PENDING,
        Ticket.TicketStatus.CHECKED_IN,
    ]

    ticket_filter = Q(
        tickets__event=event,
        tickets__status=Ticket.TicketStatus.ACTIVE,
    ) | Q(
        tickets__event=event,
        tickets__tier__payment_method__in=non_online_methods,
        tickets__status__in=valid_non_online_statuses,
    )

    rsvp_filter = Q(rsvps__event=event, rsvps__status=EventRSVP.RsvpStatus.YES)

    # Get distinct attendee IDs first, then aggregate
    # Note: .distinct() before .values().annotate() doesn't work as expected
    # because GROUP BY ignores the DISTINCT. We need to filter by IDs instead.
    attendee_ids = RevelUser.objects.filter(ticket_filter | rsvp_filter).distinct().values_list("id", flat=True)

    attendee_qs = (
        RevelUser.objects.filter(id__in=attendee_ids).values("pronouns").annotate(count=Count("id")).order_by("-count")
    )

    distribution: list[PronounCountSchema] = []
    total_with_pronouns = 0
    total_without_pronouns = 0

    for row in attendee_qs:
        pronouns = row["pronouns"]
        count = row["count"]

        if pronouns:  # Non-empty pronouns
            distribution.append(PronounCountSchema(pronouns=pronouns, count=count))
            total_with_pronouns += count
        else:
            total_without_pronouns = count

    total_attendees = total_with_pronouns + total_without_pronouns

    return EventPronounDistributionSchema(
        distribution=distribution,
        total_with_pronouns=total_with_pronouns,
        total_without_pronouns=total_without_pronouns,
        total_attendees=total_attendees,
    )
