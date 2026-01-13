"""Dietary summary aggregation for events."""

import typing as t
from collections import defaultdict

from django.db.models import Q

from accounts.models import DietaryRestriction, RevelUser, UserDietaryPreference
from events.models import Event, EventRSVP, Ticket
from events.schema import (
    AggregatedDietaryPreferenceSchema,
    AggregatedDietaryRestrictionSchema,
    EventDietarySummarySchema,
)


def get_event_dietary_summary(event: Event, user: RevelUser) -> EventDietarySummarySchema:
    """Get aggregated dietary restrictions and preferences for event attendees.

    Returns de-identified, aggregated dietary information to help with meal planning.
    Event organizers/staff see all dietary data (public + private). Regular attendees
    only see data marked as public.

    Args:
        event: The event to get dietary summary for
        user: The user requesting the summary (determines visibility filtering)

    Returns:
        EventDietarySummarySchema with aggregated restrictions and preferences
    """
    # Get all attendees regardless of viewer - visibility is controlled by is_public flag
    attendees = RevelUser.objects.filter(
        Q(tickets__event=event, tickets__status=Ticket.TicketStatus.ACTIVE)
        | Q(rsvps__event=event, rsvps__status=EventRSVP.RsvpStatus.YES)
    ).distinct()

    # Determine if user is organizer/staff
    is_organizer = (
        user.is_superuser
        or user.is_staff
        or event.organization.owner_id == user.id
        or event.organization.staff_members.filter(id=user.id).exists()
    )

    # Build filter for visibility
    visibility_filter = Q(is_public=True)
    if is_organizer:
        visibility_filter = Q()  # See everything

    # Fetch dietary restrictions with related data
    restrictions = (
        DietaryRestriction.objects.filter(user__in=attendees)
        .filter(visibility_filter)
        .select_related("food_item")
        .values_list("food_item__name", "restriction_type", "notes")
    )

    # Aggregate restrictions by (food_item, severity)
    restrictions_map: dict[tuple[str, str], dict[str, t.Any]] = defaultdict(lambda: {"count": 0, "notes": []})
    for food_item_name, restriction_type, notes in restrictions:
        key = (food_item_name, restriction_type)
        restrictions_map[key]["count"] += 1
        if notes:  # Only include non-empty notes
            restrictions_map[key]["notes"].append(notes)

    # Build aggregated restrictions response
    aggregated_restrictions = [
        AggregatedDietaryRestrictionSchema(
            food_item=food_item,
            severity=severity,  # type: ignore[arg-type]
            attendee_count=data["count"],
            notes=data["notes"],
        )
        for (food_item, severity), data in restrictions_map.items()
    ]

    # Fetch dietary preferences with related data
    preferences = (
        UserDietaryPreference.objects.filter(user__in=attendees)
        .filter(visibility_filter)
        .select_related("preference")
        .values_list("preference__name", "comment")
    )

    # Aggregate preferences by name
    preferences_map: dict[str, dict[str, t.Any]] = defaultdict(lambda: {"count": 0, "comments": []})
    for preference_name, comment in preferences:
        preferences_map[preference_name]["count"] += 1
        if comment:  # Only include non-empty comments
            preferences_map[preference_name]["comments"].append(comment)

    # Build aggregated preferences response
    aggregated_preferences = [
        AggregatedDietaryPreferenceSchema(
            name=name,
            attendee_count=data["count"],
            comments=data["comments"],
        )
        for name, data in preferences_map.items()
    ]

    return EventDietarySummarySchema(
        restrictions=aggregated_restrictions,
        preferences=aggregated_preferences,
    )
