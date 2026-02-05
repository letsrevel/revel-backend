"""Pronoun distribution aggregation for events."""

from django.db.models import Count, Q

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket
from events.schema import EventPronounDistributionSchema, PronounCountSchema


def get_event_pronoun_distribution(event: Event) -> EventPronounDistributionSchema:
    """Get aggregated pronoun distribution for event attendees.

    Attendees are defined as:
    - Users with RSVP status YES
    - Users with ACTIVE or CHECKED_IN tickets

    This matches the definition used by attendee_count and attendees() everywhere
    else in the codebase. PENDING tickets (e.g. OFFLINE) reserve capacity but are
    not yet confirmed attendees.

    Args:
        event: The event to get pronoun distribution for

    Returns:
        EventPronounDistributionSchema with distribution and totals
    """
    ticket_filter = Q(
        tickets__event=event,
        tickets__status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN],
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
