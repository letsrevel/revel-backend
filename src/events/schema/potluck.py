"""Potluck item schemas."""

from ninja import ModelSchema

from events import models


class PotluckItemCreateSchema(ModelSchema):
    item_type: models.PotluckItem.ItemTypes

    class Meta:
        model = models.PotluckItem
        fields = ["name", "item_type", "quantity", "note"]


class PotluckItemRetrieveSchema(ModelSchema):
    is_assigned: bool = False
    is_owned: bool = False

    class Meta:
        model = models.PotluckItem
        fields = ["id", "name", "item_type", "quantity", "note"]
