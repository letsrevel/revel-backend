import typing as t

from django.contrib.gis.db import models
from django.db.models import F
from tzfpy import get_tz


class CityQuerySet(models.QuerySet["City"]):
    def with_population_order(self) -> t.Self:
        """Get queryset with population order."""
        return self.order_by(F("population").desc(nulls_last=True))


class CityManager(models.Manager["City"]):
    def get_queryset(self) -> CityQuerySet:
        """Get QuerySet for City."""
        return CityQuerySet(self.model, using=self._db).with_population_order()


class City(models.Model):
    name = models.CharField(max_length=255)
    ascii_name = models.CharField(max_length=255)
    country = models.CharField(max_length=255)
    iso2 = models.CharField(max_length=2)
    iso3 = models.CharField(max_length=3)
    admin_name = models.CharField(max_length=255, blank=True, null=True)
    capital = models.CharField(max_length=32, blank=True, null=True)
    population = models.BigIntegerField(blank=True, null=True)
    city_id = models.BigIntegerField(unique=True)
    location = models.PointField(geography=True)
    timezone = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    objects = CityManager()

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Auto-populate timezone from location coordinates if not set."""
        if not self.timezone and self.location:
            # get_tz expects (longitude, latitude)
            self.timezone = get_tz(self.location.x, self.location.y)
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ascii_name", "iso2", "location"], name="geo_city_unique_city_location_per_country"
            ),
        ]
        indexes = [
            models.Index(fields=["ascii_name"], name="geo_city_ascii_name"),
            models.Index(fields=["iso2"], name="geo_city_iso2"),
            models.Index(fields=["ascii_name", "iso2"], name="geo_city_ascii_name_iso2"),
            models.Index(fields=["population"], name="geo_city_population"),
            models.Index(fields=["location"], name="geo_city_location"),
        ]
        ordering = ["-population"]
        verbose_name_plural = "cities"

    def __str__(self) -> str:
        parts = [self.name, self.admin_name, self.country]
        return ", ".join(p for p in parts if p)
