"""Controller for managing user follows (organizations and event series)."""

from django.db.models import QuerySet
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from events import schema
from events.models.follow import EventSeriesFollow, OrganizationFollow
from events.service import follow_service


@api_controller("/me/following", auth=I18nJWTAuth(), tags=["Following"])
class FollowingController(UserAwareController):
    """Controller for managing user's follows."""

    @route.get(
        "/organizations",
        url_name="list_followed_organizations",
        response=PaginatedResponseSchema[schema.OrganizationFollowSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_followed_organizations(self) -> QuerySet[OrganizationFollow]:
        """List all organizations you're following.

        Returns a paginated list of followed organizations with notification preferences.
        Only active follows are returned (not archived/unfollowed).
        """
        return follow_service.get_user_followed_organizations(self.user())

    @route.get(
        "/event-series",
        url_name="list_followed_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesFollowSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_followed_event_series(self) -> QuerySet[EventSeriesFollow]:
        """List all event series you're following.

        Returns a paginated list of followed event series with notification preferences.
        Only active follows are returned (not archived/unfollowed).
        """
        return follow_service.get_user_followed_event_series(self.user())
