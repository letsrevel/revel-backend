import typing as t

from django.db.models import QuerySet
from ninja import Query
from ninja_extra import ControllerBase, api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.throttling import GeoThrottle
from geo.filters import CityFilterSchema
from geo.models import City
from geo.schema import CitySchema
from geo.service import list_countries


@api_controller("/cities", throttle=GeoThrottle())
class CityController(ControllerBase):
    def get_queryset(self) -> QuerySet[City]:
        """Get the base queryset for Cities."""
        return City.objects.all()

    @route.get("/", response=PaginatedResponseSchema[CitySchema], url_name="list_cities")
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["name", "ascii_name", "country"],
    )
    def list_cities(self, filters: CityFilterSchema = Query(...)) -> QuerySet[City]:  # type: ignore[type-arg]
        """List all cities."""
        return filters.filter(self.get_queryset())

    @route.get("/countries", response=list[str], url_name="list_countries")
    def list_countries(self) -> list[str]:
        """List all countries."""
        return list_countries()

    @route.get("/{city_id}", response=CitySchema, url_name="get_city")
    def get_city(self, city_id: int) -> City:
        """Get a city by its ID."""
        return t.cast(City, self.get_object_or_exception(self.get_queryset(), city_id=city_id))
