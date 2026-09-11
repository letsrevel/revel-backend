from functools import lru_cache

from geo.models import City


@lru_cache
def list_countries() -> list[str]:
    """Cached method to list countries."""
    return list(City.objects.order_by("country").values_list("country", flat=True).distinct())
