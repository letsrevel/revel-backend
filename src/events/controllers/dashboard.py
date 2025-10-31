# src/events/controllers/dashboard.py
import typing as t
from uuid import UUID

from django.db.models import Q, QuerySet
from django.utils import timezone
from ninja import Query
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from ninja_jwt.authentication import JWTAuth

from accounts.models import RevelUser
from events import filters, models, schema
from events.controllers.user_aware_controller import UserAwareController


@api_controller("/dashboard", auth=JWTAuth())
class DashboardController(UserAwareController):
    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    def get_event_queryset(self) -> QuerySet[models.Event]:
        """Get the event queryset."""
        return models.Event.objects.for_user(self.user())

    def get_event_series_queryset(self) -> QuerySet[models.EventSeries]:
        """Get the event series queryset."""
        return models.EventSeries.objects.for_user(self.user())

    def get_organization_queryset(self) -> QuerySet[models.Organization]:
        """Get the organization queryset."""
        return models.Organization.objects.for_user(self.user())

    @route.get(
        "/organizations",
        url_name="dashboard_organizations",
        response=PaginatedResponseSchema[schema.OrganizationRetrieveSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "tags__tag__name"])
    def dashboard_organizations(
        self,
        params: filters.DashboardOrganizationsFiltersSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Organization]:
        """View organizations for your dashboard filtered by your relationship to them.

        Filter by: organizations you own, are staff of, are a member of, or have pending requests to.
        Shows only organizations you have permission to view. Use this to display "My Organizations"
        sections in the UI.
        """
        user = self.user()

        # 1. Get IDs of all orgs user is AUTHORIZED to see.
        authorized_org_ids = self.get_organization_queryset().values("id")

        # 2. Get IDs of all orgs matching the dashboard filter RELATIONSHIPS.
        relationship_org_ids = params.get_organizations_queryset(user.id).values("id")

        # 3. Find the INTERSECTION of the two sets of IDs.
        final_org_ids = authorized_org_ids.intersection(relationship_org_ids)

        # 4. Fetch the final, full Organization objects. The decorators will handle pagination.
        return models.Organization.objects.full().filter(id__in=final_org_ids).distinct()

    @route.get("/events", url_name="dashboard_events", response=PaginatedResponseSchema[schema.EventInListSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=[
            "name",
            "description",
            "event_series__name",
            "event_series__description",
            "organization__name",
            "organization__description",
            "tags__tag__name",
        ],
    )
    def dashboard_events(
        self,
        params: filters.DashboardEventsFiltersSchema = Query(...),  # type: ignore[type-arg]
        order_by: t.Literal["start", "-start"] = "start",
    ) -> QuerySet[models.Event]:
        """View upcoming events for your dashboard filtered by your relationship to them.

        Filter by: events you're organizing, attending (RSVP'd or have tickets), invited to, or have
        requested invitations to. Only shows future events you have permission to view. Use this to
        display "My Events" sections in the UI.
        """
        user = self.user()

        # 1. Get IDs of all events the user is AUTHORIZED to see.
        authorized_event_ids = self.get_event_queryset().values("id")

        # 2. Get IDs of all events that match the dashboard's relationship filters.
        relationship_event_ids = params.get_events_queryset(user.id).values("id")

        # 3. Find the INTERSECTION of the two sets of IDs.
        final_event_ids = authorized_event_ids.intersection(relationship_event_ids)

        # 4. Fetch the final, full Event objects based on the correct IDs.
        qs = models.Event.objects.full().filter(id__in=final_event_ids)

        today = timezone.now().date()
        qs = qs.filter(Q(start__date__gte=today) | Q(start__isnull=True))

        if not user.is_staff:
            qs = qs.exclude(status=models.Event.Status.DRAFT)

        return qs.distinct().order_by(order_by)

    @route.get(
        "/event_series",
        url_name="dashboard_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesRetrieveSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "tags__tag__name"])
    def dashboard_event_series(
        self,
        params: filters.DashboardEventSeriesFiltersSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventSeries]:
        """View event series for your dashboard filtered by your relationship to them.

        Filter by: series you're organizing or series you're attending events in. Shows only
        series you have permission to view. Use this to display "My Series" sections in the UI.
        """
        user = self.user()

        # 1. Get IDs of all event series the user is AUTHORIZED to see.
        authorized_series_ids = self.get_event_series_queryset().values("id")

        # 2. Get IDs of all event series that match the dashboard's relationship filters.
        relationship_series_ids = params.get_event_series_queryset(user.id).values("id")

        # 3. Find the INTERSECTION of the two sets of IDs.
        final_series_ids = authorized_series_ids.intersection(relationship_series_ids)

        # 4. Fetch the final, full EventSeries objects based on the correct IDs.
        return models.EventSeries.objects.full().filter(id__in=final_series_ids).distinct()

    @route.get(
        "/invitations",
        url_name="dashboard_invitations",
        response=PaginatedResponseSchema[schema.MyEventInvitationSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "custom_message"])
    def dashboard_invitations(
        self,
        event_id: UUID | None = None,
        include_past: bool = False,
    ) -> QuerySet[models.EventInvitation]:
        """View your event invitations across all events.

        Returns invitations you've received with event details and any special privileges granted
        (tier assignment, waived requirements, etc.). By default shows only invitations for upcoming
        events; set include_past=true to include past events. An event is considered past if its end
        time has passed. Filter by event_id to see invitations for a specific event.
        """
        qs = models.EventInvitation.objects.with_event_details().filter(user=self.user())

        if event_id:
            qs = qs.filter(event_id=event_id)

        if not include_past:
            # Filter for upcoming events: end > now
            qs = qs.filter(event__end__gt=timezone.now())

        return qs.distinct().order_by("-created_at")

    @route.get(
        "/tickets",
        url_name="dashboard_tickets",
        response=PaginatedResponseSchema[schema.UserTicketSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "tier__name"])
    def dashboard_tickets(
        self,
        params: filters.TicketFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Ticket]:
        """View your tickets across all events.

        Returns all your tickets with their current status and event details.
        By default, shows only tickets for upcoming events; set include_past=true
        to include past events. An event is considered past if its end time has passed.
        Supports filtering by status (pending/active/cancelled/checked_in) and
        payment method. Results are ordered by newest first.
        """
        qs = models.Ticket.objects.select_related("event", "tier").filter(user=self.user()).order_by("-created_at")
        return params.filter(qs).distinct()

    @route.get(
        "/invitation-requests",
        url_name="dashboard_invitation_requests",
        response=PaginatedResponseSchema[schema.EventInvitationRequestSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def dashboard_invitation_requests(
        self,
        event_id: UUID | None = None,
        params: filters.InvitationRequestFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventInvitationRequest]:
        """View your invitation requests across all events.

        Returns your invitation requests with their current status. By default shows only pending
        requests; use ?status=approved or ?status=rejected to see decided requests, or omit the
        status parameter to see all requests. Filter by event_id to see requests for a specific
        event. Use this to track which events you've requested access to.
        """
        qs = models.EventInvitationRequest.objects.select_related("event").filter(user=self.user())
        if event_id:
            qs = qs.filter(event_id=event_id)
        return params.filter(qs).distinct()

    @route.get(
        "/rsvps",
        url_name="dashboard_rsvps",
        response=PaginatedResponseSchema[schema.UserRSVPSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description"])
    def dashboard_rsvps(
        self,
        params: filters.RSVPFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventRSVP]:
        """View your RSVPs across all events.

        Returns all your RSVPs with their current status and event details.
        By default, shows only RSVPs for upcoming events; set include_past=true
        to include past events. An event is considered past if its end time has passed.
        Supports filtering by status (yes/no/maybe). Results are ordered by newest first.
        """
        qs = models.EventRSVP.objects.select_related("event").filter(user=self.user()).order_by("-created_at")
        return params.filter(qs).distinct()
