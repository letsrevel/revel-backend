from django.contrib.gis.db import models

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TimeStampedModel


class PotluckItem(TimeStampedModel):
    class ItemTypes(models.TextChoices):
        FOOD = "food", "Food"
        MAIN_COURSE = "main_course", "Main Course"
        SIDE_DISH = "side_dish", "Side Dish"
        DESSERT = "dessert", "Dessert"
        DRINK = "drink", "Drink"
        ALCOHOL = "alcohol", "Alcohol"
        NON_ALCOHOLIC = "non_alcoholic", "Non-Alcoholic"
        SUPPLIES = "supplies", "Supplies"  # cups, napkins, etc.
        LABOR = "labor", "Labor / Help"  # setup, cleanup, etc.
        ENTERTAINMENT = "entertainment", "Entertainment"  # music, games, performance
        SEXUAL_HEALTH = "sexual_health", "Sexual Health"  # condoms, lube, gloves
        TOYS = "toys", "Toys"
        CARE = "care", "Care"  # blankets, snacks, water, comfort stuff
        TRANSPORT = "transport", "Transport / Shuttle"  # offer a ride etc.
        MISC = "misc", "Miscellaneous"

    created_by = models.ForeignKey(RevelUser, on_delete=models.SET_NULL, null=True, blank=True)
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="potluck_items")
    name = models.CharField(max_length=100, db_index=True)
    quantity = models.CharField(max_length=20, blank=True, null=True)
    item_type = models.CharField(choices=ItemTypes.choices, max_length=20, db_index=True)
    note = MarkdownField(null=True, blank=True)
    is_suggested = models.BooleanField(default=False, help_text="For host-created items awaiting volunteers")
    assignee = models.ForeignKey(
        RevelUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="potluck_items"
    )

    def __str__(self) -> str:
        return f"{self.name} ({self.item_type})"
