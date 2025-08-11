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
        """List and search for existing tags.

        Supports autocomplete by using the `search` query parameter.
        Example: `/api/tags/?search=comm`
        """
        return Tag.objects.all()
