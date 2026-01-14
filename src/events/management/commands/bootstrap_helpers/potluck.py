# src/events/management/commands/bootstrap_helpers/potluck.py
"""Potluck item creation for bootstrap process."""

import typing as t

import structlog

from accounts.models import RevelUser
from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_potluck_items(state: BootstrapState) -> None:
    """Create potluck items for potluck-enabled events."""
    logger.info("Creating potluck items...")

    # Spring Potluck items
    potluck_items = [
        # Host-suggested items (unassigned)
        {
            "name": "Main Course (pasta, casserole, etc)",
            "quantity": "Serves 8-10",
            "item_type": events_models.PotluckItem.ItemTypes.MAIN_COURSE,
            "is_suggested": True,
            "note": "We need 3-4 main dishes. Please label ingredients for allergies!",
        },
        {
            "name": "Fresh Garden Salad",
            "quantity": "Large bowl",
            "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
            "is_suggested": True,
            "note": "Fresh, seasonal veggies appreciated",
        },
        {
            "name": "Dessert",
            "quantity": "Serves 8-10",
            "item_type": events_models.PotluckItem.ItemTypes.DESSERT,
            "is_suggested": True,
            "note": "Sweet treats welcome! Cookies, cake, pie, etc.",
        },
        {
            "name": "Beverages (non-alcoholic)",
            "quantity": "2-3 liters",
            "item_type": events_models.PotluckItem.ItemTypes.NON_ALCOHOLIC,
            "is_suggested": True,
            "note": "Juice, lemonade, iced tea, etc.",
        },
        {
            "name": "Paper plates, cups, napkins",
            "quantity": "For 80 people",
            "item_type": events_models.PotluckItem.ItemTypes.SUPPLIES,
            "is_suggested": True,
            "note": "Compostable/recyclable preferred!",
        },
        {
            "name": "Setup Help",
            "quantity": "2-3 volunteers",
            "item_type": events_models.PotluckItem.ItemTypes.LABOR,
            "is_suggested": True,
            "note": "Arrive 30 min early to help set up tables",
        },
        # User-contributed items (assigned)
        {
            "name": "Homemade Lasagna",
            "quantity": "Serves 10",
            "item_type": events_models.PotluckItem.ItemTypes.MAIN_COURSE,
            "is_suggested": False,
            "assignee": state.users["attendee_1"],
            "created_by": state.users["attendee_1"],
            "note": "Vegetarian option with spinach and ricotta",
        },
        {
            "name": "Mediterranean Mezze Platter",
            "quantity": "Large platter",
            "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
            "is_suggested": False,
            "assignee": state.users["attendee_2"],
            "created_by": state.users["attendee_2"],
            "note": "Hummus, falafel, olives, pita bread - all vegan!",
        },
        {
            "name": "Fresh Fruit Salad",
            "quantity": "Serves 12",
            "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
            "is_suggested": False,
            "assignee": state.users["multi_org_user"],
            "created_by": state.users["multi_org_user"],
            "note": "Seasonal berries and melons",
        },
        {
            "name": "Chocolate Brownies",
            "quantity": "2 dozen",
            "item_type": events_models.PotluckItem.ItemTypes.DESSERT,
            "is_suggested": False,
            "assignee": state.users["attendee_3"],
            "created_by": state.users["attendee_3"],
            "note": "Homemade fudgy brownies!",
        },
        {
            "name": "Fresh Lemonade",
            "quantity": "3 liters",
            "item_type": events_models.PotluckItem.ItemTypes.NON_ALCOHOLIC,
            "is_suggested": False,
            "assignee": state.users["org_alpha_member"],
            "created_by": state.users["org_alpha_member"],
            "note": "Freshly squeezed with mint",
        },
        {
            "name": "Acoustic Guitar Performance",
            "quantity": "30 min set",
            "item_type": events_models.PotluckItem.ItemTypes.ENTERTAINMENT,
            "is_suggested": False,
            "assignee": state.users["attendee_4"],
            "created_by": state.users["attendee_4"],
            "note": "Folk and acoustic covers - let me know preferred time!",
        },
    ]

    for item_data in potluck_items:
        assignee = t.cast(RevelUser | None, item_data.pop("assignee", None))
        created_by = t.cast(RevelUser | None, item_data.pop("created_by", None))

        events_models.PotluckItem.objects.create(
            event=state.events["spring_potluck"],
            assignee=assignee,
            created_by=created_by,
            **item_data,
        )

    logger.info(f"Created {len(potluck_items)} potluck items")
