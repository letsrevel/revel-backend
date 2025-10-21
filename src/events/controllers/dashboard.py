# src/events/controllers/dashboard.py
import typing as t

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

    def get_invitations_queryset(self) -> QuerySet[models.EventInvitation]:
        """Get the pending invitations queryset, sorted by event date (sooner first)."""
        return models.EventInvitation.objects.for_user(self.user())

    @route.get(
        "/organizations",
        url_name="dashboard_organizations",
        response=PaginatedResponseSchema[schema.OrganizationRetrieveSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
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
        return models.Organization.objects.filter(id__in=final_org_ids)

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
        qs = models.Event.objects.filter(id__in=final_event_ids)

        # 5. Apply any remaining display logic.
        qs = qs.select_related("organization", "event_series")
        today = timezone.now().date()
        qs = qs.filter(Q(start__date__gte=today) | Q(start__isnull=True))

        if not user.is_staff:
            qs = qs.exclude(status=models.Event.Status.DRAFT)

        return qs.order_by(order_by)

    @route.get(
        "/event_series",
        url_name="dashboard_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesRetrieveSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
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
        return models.EventSeries.objects.filter(id__in=final_series_ids)

    @route.get(
        "/invitations",
        url_name="dashboard_invitations",
        response=PaginatedResponseSchema[schema.InvitationSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description"])
    def dashboard_invitations(
        self,
    ) -> QuerySet[models.EventInvitation]:
        """View your pending event invitations.

        Returns invitations you've received but not yet acted on, sorted by event date (soonest first).
        Use this to display a "Pending Invitations" section prompting users to RSVP or purchase tickets.
        """
        return self.get_invitations_queryset()
