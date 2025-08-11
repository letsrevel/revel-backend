from ninja import FilterSchema


class CityFilterSchema(FilterSchema):
    country: str | None = None
