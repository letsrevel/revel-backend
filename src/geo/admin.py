from django.contrib import admin
from unfold.admin import ModelAdmin

from . import models


@admin.register(models.City)
class CityAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin model for the City model."""

    list_display = [
        "name",
        "admin_name",
        "country",
        "population",
        "location",
    ]
    search_fields = ["name", "ascii_name", "country"]
    list_filter = ["country", "capital"]
