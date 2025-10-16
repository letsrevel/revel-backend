# src/api/controllers/tags.py (a new file)
from django.db.models import QuerySet
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.models import Tag
from common.schema import TagSchema
from events.controllers.user_aware_controller import UserAwareController


@api_controller("/tags", tags=["Tags"])
class TagController(UserAwareController):
    @route.get("/", url_name="list_tags", response=PaginatedResponseSchema[TagSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_tags(self) -> QuerySet[Tag]:
        """Browse and search all available tags in the system.

        Tags are used to categorize organizations, events, and series. Supports autocomplete via
        the 'search' query parameter (e.g., /api/tags/?search=tech). Use this to populate tag
        selection dropdowns or filters.
        """
        return Tag.objects.all()
