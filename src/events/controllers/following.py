"""Controller for managing user follows (organizations and event series)."""

from uuid import UUID

from django.db.models import QuerySet
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import ScopedJWTAuth
from common.controllers import UserAwareController
from events import schema
from events.models.follow import EventSeriesFollow, OrganizationFollow
from events.service import follow_service
from oauth.permissions import RequireScope


@api_controller("/me/following", auth=ScopedJWTAuth(), tags=["Following"], permissions=[RequireScope("me:read")])
class FollowingController(UserAwareController):
    """Controller for managing user's follows."""

    @route.get(
        "/organizations",
        url_name="list_followed_organizations",
        response=PaginatedResponseSchema[schema.OrganizationFollowSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_followed_organizations(self, organization: UUID | None = None) -> QuerySet[OrganizationFollow]:
        """List all organizations you're following.

        Returns a paginated list of followed organizations with notification preferences.
        Only active follows are returned (not archived/unfollowed). Pass ``organization``
        to list only the follow of one organization.
        """
        return follow_service.get_user_followed_organizations(self.user(), organization=organization)

    @route.get(
        "/event-series",
        url_name="list_followed_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesFollowSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_followed_event_series(self, organization: UUID | None = None) -> QuerySet[EventSeriesFollow]:
        """List all event series you're following.

        Returns a paginated list of followed event series with notification preferences.
        Only active follows are returned (not archived/unfollowed). Pass ``organization``
        to list only the series of one organization.
        """
        return follow_service.get_user_followed_event_series(self.user(), organization=organization)
