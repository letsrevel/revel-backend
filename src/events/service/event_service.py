import typing as t
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import DietaryRestriction, RevelUser, UserDietaryPreference
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventRSVP,
    EventToken,
    Ticket,
    TicketTier,
)
from events.models.mixins import LocationMixin
from events.schema import (
    AggregatedDietaryPreferenceSchema,
    AggregatedDietaryRestrictionSchema,
    EventDietarySummarySchema,
    InvitationBaseSchema,
)

T = t.TypeVar("T", bound=LocationMixin)


def calculate_calendar_date_range(
    week: int | None = None,
    month: int | None = None,
    year: int | None = None,
) -> tuple[datetime, datetime]:
    """Calculate start and end datetime for calendar views.

    Args:
        week: ISO week number (1-53), requires year
        month: Month number (1-12), requires year
        year: Year (e.g., 2025)

    Returns:
        Tuple of (start_datetime, end_datetime) representing the time range.
        If no parameters provided, returns current month range.

    Priority: week > month > year > current_month
    """
    now = timezone.now()
    utc = ZoneInfo("UTC")

    if week is not None:
        # ISO week: Week containing first Thursday = Week 1
        # Jan 4 is always in Week 1 (anchor point for calculation)
        target_year = year or now.year
        jan_4 = datetime(target_year, 1, 4, tzinfo=utc)
        week_1_start = jan_4 - timedelta(days=jan_4.isoweekday() - 1)
        start_datetime = week_1_start + timedelta(weeks=week - 1)
        end_datetime = start_datetime + timedelta(weeks=1)
        return start_datetime, end_datetime

    if month is not None:
        target_year = year or now.year
        start_datetime = datetime(target_year, month, 1, tzinfo=utc)
        next_month_year = target_year + 1 if month == 12 else target_year
        next_month = 1 if month == 12 else month + 1
        end_datetime = datetime(next_month_year, next_month, 1, tzinfo=utc)
        return start_datetime, end_datetime

    if year is not None:
        start_datetime = datetime(year, 1, 1, tzinfo=utc)
        end_datetime = datetime(year + 1, 1, 1, tzinfo=utc)
        return start_datetime, end_datetime

    # Default: current month
    start_datetime = datetime(now.year, now.month, 1, tzinfo=utc)
    next_month_year = now.year + 1 if now.month == 12 else now.year
    next_month = 1 if now.month == 12 else now.month + 1
    end_datetime = datetime(next_month_year, next_month, 1, tzinfo=utc)
    return start_datetime, end_datetime


def order_by_distance(point: Point | None, queryset: QuerySet[T]) -> QuerySet[T]:
    """Get cities by ip."""
    if point is None:
        return queryset

    return queryset.annotate(  # type: ignore[no-any-return]
        distance=Distance("location", point),
    ).order_by("distance")


def create_event_token(
    *,
    event: Event,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    invitation_payload: InvitationBaseSchema | None = None,
    ticket_tier_id: UUID | None = None,
    name: str | None = None,
    grants_invitation: bool = False,
    max_uses: int = 0,
) -> EventToken:
    """Get a temporary JWT.

    This will need to be used by a user in combination with their OTP code to obtain a valid JWT.
    """
    duration = timedelta(minutes=duration) if isinstance(duration, int) else duration
    return EventToken.objects.create(
        name=name,
        issuer=issuer,
        event=event,
        expires_at=timezone.now() + duration,
        max_uses=max_uses,
        ticket_tier_id=ticket_tier_id,
        grants_invitation=grants_invitation,
        invitation_payload=invitation_payload.model_dump(mode="json") if invitation_payload is not None else None,
    )


def get_event_token(token: str) -> EventToken | None:
    """Retrieves an EventToken from a JWT."""
    return (
        EventToken.objects.select_related("event")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )


@transaction.atomic
def claim_invitation(user: RevelUser, token: str) -> EventInvitation | None:
    """Claim an invitation given an Event JWT."""
    event_token = get_event_token(token)
    if event_token is None:
        return None
    if not event_token.grants_invitation:
        return None
    if event_token.max_uses and event_token.uses >= event_token.max_uses:
        return None
    # warning: do not save the event_token object now. If pop() is removed get_or_create will fail)
    invitation, created = EventInvitation.objects.get_or_create(
        event=event_token.event,
        user=user,
        defaults={
            "tier_id": event_token.ticket_tier_id,
            **(event_token.invitation_payload or {}),
        },
    )
    if created:
        EventToken.objects.filter(pk=event_token.pk).update(uses=F("uses") + 1)
    return invitation


def create_invitation_request(event: Event, user: RevelUser, message: str | None = None) -> EventInvitationRequest:
    """Create an invitation request.

    Args:
        event: The event to request an invitation for.
        user: The user requesting the invitation.
        message: Optional message from the user explaining why they want to attend.

    Returns:
        The created EventInvitationRequest.

    Raises:
        HttpError: If the event does not accept invitation requests, the user is already invited,
                  or a pending request already exists.
    """
    if not event.accept_invitation_requests:
        raise HttpError(400, str(_("This event does not accept invitation requests.")))

    if EventInvitation.objects.filter(event=event, user=user).exists():
        raise HttpError(400, str(_("You are already invited to this event.")))

    if EventInvitationRequest.objects.filter(
        event=event, user=user, status=EventInvitationRequest.InvitationRequestStatus.PENDING
    ).exists():
        raise HttpError(400, str(_("You have already requested an invitation to this event.")))

    return EventInvitationRequest.objects.create(event=event, user=user, message=message)


@transaction.atomic
def approve_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser, tier: TicketTier | None = None
) -> EventInvitationRequest:
    """Approve an invitation request."""
    invitation_request.status = EventInvitationRequest.InvitationRequestStatus.APPROVED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status", "decided_by"])
    EventInvitation.objects.get_or_create(event=invitation_request.event, user=invitation_request.user, tier=tier)
    return invitation_request


def reject_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser
) -> EventInvitationRequest:
    """Reject an invitation request."""
    invitation_request.status = EventInvitationRequest.InvitationRequestStatus.REJECTED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status", "decided_by"])
    return invitation_request


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
