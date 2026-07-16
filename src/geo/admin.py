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
        "iso2",
        "timezone",
        "population",
    ]
    search_fields = ["name", "ascii_name", "country", "iso2"]
    list_filter = ["country", "capital"]
    list_per_page = 50
    show_full_result_count = False
