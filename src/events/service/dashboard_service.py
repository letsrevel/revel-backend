"""Dashboard query-composition helpers.

Centralises the "authorized ∩ relationship" intersection logic used by the
dashboard endpoints, plus the invitation-exclusion chain. Materialising the
two ID sets in Python (rather than using SQL ``INTERSECT`` or nested
subqueries) keeps pagination ``COUNT(*)`` fast on large datasets.
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import RevelUser
from events import filters, models


def get_user_related_events(
    user: RevelUser,
    params: filters.DashboardEventsFiltersSchema,
    *,
    include_past: bool = False,
) -> QuerySet[models.Event]:
    """Return events the user is both authorised to see AND related to.

    The result is the set intersection of:

    1. Events the user has visibility permission on.
    2. Events matching the dashboard relationship filters
       (owner / staff / member / rsvp / tickets / invitations / bookmarks).

    IDs are materialised in Python to keep the final ``WHERE id IN (...)``
    pagination-friendly. Optionally narrows further by ``requires_ticket``.

    Args:
        user: The authenticated user.
        params: Dashboard event filter schema.
        include_past: Whether the authorised set should include past events.

    Returns:
        A queryset of full Event objects matching the intersection.
    """
    authorized_event_ids = set(
        models.Event.objects.for_user(user, include_past=include_past).values_list("id", flat=True)
    )
    relationship_event_ids = set(params.get_events_queryset(user.id).values_list("id", flat=True))
    final_event_ids = authorized_event_ids & relationship_event_ids

    qs = models.Event.objects.full().filter(id__in=list(final_event_ids)).with_user_bookmark(user)
    if params.requires_ticket is not None:
        qs = qs.filter(requires_ticket=params.requires_ticket)
    return qs


def get_user_related_organizations(
    user: RevelUser,
    params: filters.DashboardOrganizationsFiltersSchema,
) -> QuerySet[models.Organization]:
    """Return organisations the user is both authorised to see AND related to.

    Args:
        user: The authenticated user.
        params: Dashboard organisation filter schema.

    Returns:
        A queryset of full Organization objects matching the intersection.
    """
    authorized_org_ids = set(models.Organization.objects.for_user(user).values_list("id", flat=True))
    relationship_org_ids = set(params.get_organizations_queryset(user.id).values_list("id", flat=True))
    final_org_ids = authorized_org_ids & relationship_org_ids
    return models.Organization.objects.full().filter(id__in=list(final_org_ids))


def get_user_related_event_series(
    user: RevelUser,
    params: filters.DashboardEventSeriesFiltersSchema,
) -> QuerySet[models.EventSeries]:
    """Return event series the user is both authorised to see AND related to.

    Args:
        user: The authenticated user.
        params: Dashboard event-series filter schema.

    Returns:
        A queryset of full EventSeries objects matching the intersection.
    """
    authorized_series_ids = set(models.EventSeries.objects.for_user(user).values_list("id", flat=True))
    relationship_series_ids = set(params.get_event_series_queryset(user.id).values_list("id", flat=True))
    final_series_ids = authorized_series_ids & relationship_series_ids
    return models.EventSeries.objects.full().filter(id__in=list(final_series_ids))


def get_user_invitations(
    user: RevelUser,
    *,
    event_id: UUID | None = None,
    include_past: bool = False,
    exclude_accepted: bool = True,
) -> QuerySet[models.EventInvitation]:
    """Return event invitations addressed to ``user`` with dashboard filtering.

    By default, hides past events and invitations for events where the user
    has already accepted (either a non-cancelled ticket or a YES RSVP).

    Args:
        user: The authenticated user.
        event_id: Optionally restrict to invitations for a single event.
        include_past: Include invitations whose events have ended.
        exclude_accepted: Hide invitations the user has effectively accepted.

    Returns:
        A queryset of EventInvitation rows, ordered newest-first.
    """
    qs = models.EventInvitation.objects.with_event_details().filter(user=user)

    if event_id:
        qs = qs.filter(event_id=event_id)

    if not include_past:
        qs = qs.filter(event__end__gt=timezone.now())

    if exclude_accepted:
        qs = qs.exclude(
            event_id__in=models.Ticket.objects.filter(
                user=user,
                status__in=[
                    models.Ticket.TicketStatus.PENDING,
                    models.Ticket.TicketStatus.ACTIVE,
                    models.Ticket.TicketStatus.CHECKED_IN,
                ],
            ).values("event_id")
        ).exclude(
            event_id__in=models.EventRSVP.objects.filter(
                user=user,
                status=models.EventRSVP.RsvpStatus.YES,
            ).values("event_id")
        )

    return qs.distinct().order_by("-created_at")
