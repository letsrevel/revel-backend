from functools import lru_cache

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db.models import F, FloatField, QuerySet, Value
from django.db.models.functions import Coalesce, Sqrt

from geo.ip2 import resolve_ip_to_point
from geo.models import City


def get_cities_by_ip(ip_address: str, max_radius: int = 300) -> QuerySet[City]:
    """Get cities by ip."""
    point = resolve_ip_to_point(ip_address)
    if not point:
        return City.objects.all()

    return (
        City.objects.filter(location__distance_lte=(point, D(km=max_radius)))
        .annotate(
            distance=Distance("location", point),
            pop_weight=Sqrt(Coalesce(F("population"), Value(1), output_field=FloatField())),
        )
        .annotate(score=F("pop_weight") / (F("distance") + Value(1.0)))
        .order_by("-score")
    )


@lru_cache
def list_countries() -> list[str]:
    """Cached method to list countries."""
    return list(City.objects.order_by("country").values_list("country", flat=True).distinct())
