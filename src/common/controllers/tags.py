# src/api/controllers/tags.py (a new file)
from django.db.models import Count, QuerySet
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.controllers import UserAwareController
from common.models import Tag
from common.schema import TagSchema


@api_controller("/tags", tags=["Tags"])
class TagController(UserAwareController):
    @route.get("/", url_name="list_tags", response=PaginatedResponseSchema[TagSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_tags(self) -> QuerySet[Tag]:
        """Browse and search all available tags in the system.

        Tags are used to categorize organizations, events, and series. Supports autocomplete via
        the 'search' query parameter (e.g., /api/tags/?search=tech). Use this to populate tag
        selection dropdowns or filters. Results are ordered by popularity (most used first).
        """
        return Tag.objects.annotate(usage_count=Count("assignments")).order_by("-usage_count", "name")
