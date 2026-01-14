# src/events/management/commands/bootstrap_helpers/dietary.py
"""Dietary preferences and restrictions creation for bootstrap process."""

import structlog

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    UserDietaryPreference,
)

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_dietary_data(state: BootstrapState) -> None:
    """Create dietary preferences and restrictions for users."""
    logger.info("Creating dietary preferences and restrictions...")

    # Get dietary preferences (seeded in migration)
    preferences = _get_dietary_preferences()
    food_items = _get_food_items()

    _create_alice_dietary(state, preferences, food_items)
    _create_bob_dietary(state, preferences)
    _create_charlie_dietary(state, preferences, food_items)
    _create_diana_dietary(state, food_items)
    _create_eve_dietary(state, preferences, food_items)
    _create_frank_dietary(state, preferences)
    _create_george_dietary(state, food_items)
    _create_hannah_dietary(state, preferences, food_items)
    _create_ivan_dietary(state, food_items)
    _create_julia_dietary(state, preferences, food_items)
    _create_karen_dietary(state, preferences)
    _create_leo_dietary(state, food_items)
    _create_maria_dietary(state, preferences)

    logger.info("Created dietary preferences and restrictions for users")


def _get_dietary_preferences() -> dict[str, DietaryPreference]:
    """Get dietary preferences by name."""
    return {
        "vegan": DietaryPreference.objects.get(name="Vegan"),
        "vegetarian": DietaryPreference.objects.get(name="Vegetarian"),
        "gluten_free": DietaryPreference.objects.get(name="Gluten-Free"),
        "dairy_free": DietaryPreference.objects.get(name="Dairy-Free"),
        "pescatarian": DietaryPreference.objects.get(name="Pescatarian"),
        "halal": DietaryPreference.objects.get(name="Halal"),
        "kosher": DietaryPreference.objects.get(name="Kosher"),
    }


def _get_food_items() -> dict[str, FoodItem]:
    """Get or create food items."""
    items = {}
    food_names = ["Peanuts", "Shellfish", "Tree nuts", "Gluten", "Milk", "Eggs", "Soy", "Sesame", "Fish", "Celery"]
    for name in food_names:
        items[name.lower().replace(" ", "_")], _ = FoodItem.objects.get_or_create(name=name)
    return items


def _create_alice_dietary(
    state: BootstrapState, preferences: dict[str, DietaryPreference], food_items: dict[str, FoodItem]
) -> None:
    """Alice (org_alpha_owner) - Vegetarian with mild lactose intolerance."""
    UserDietaryPreference.objects.create(
        user=state.users["org_alpha_owner"],
        preference=preferences["vegetarian"],
        comment="Vegetarian for 5 years, prefer organic when possible",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=state.users["org_alpha_owner"],
        food_item=food_items["milk"],
        restriction_type=DietaryRestriction.RestrictionType.INTOLERANT,
        notes="Mild lactose intolerance, can handle small amounts in cooked food",
        is_public=True,
    )


def _create_bob_dietary(state: BootstrapState, preferences: dict[str, DietaryPreference]) -> None:
    """Bob (org_alpha_staff) - Vegan."""
    UserDietaryPreference.objects.create(
        user=state.users["org_alpha_staff"],
        preference=preferences["vegan"],
        comment="Strict vegan, no animal products including honey",
        is_public=True,
    )


def _create_charlie_dietary(
    state: BootstrapState, preferences: dict[str, DietaryPreference], food_items: dict[str, FoodItem]
) -> None:
    """Charlie (org_alpha_member) - Gluten-Free due to celiac."""
    UserDietaryPreference.objects.create(
        user=state.users["org_alpha_member"],
        preference=preferences["gluten_free"],
        comment="Celiac disease, need strict gluten-free options",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=state.users["org_alpha_member"],
        food_item=food_items["gluten"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Celiac disease - even cross-contamination is an issue",
        is_public=True,
    )


def _create_diana_dietary(state: BootstrapState, food_items: dict[str, FoodItem]) -> None:
    """Diana (org_beta_owner) - Severe peanut allergy."""
    DietaryRestriction.objects.create(
        user=state.users["org_beta_owner"],
        food_item=food_items["peanuts"],
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Anaphylaxis risk, carries EpiPen",
        is_public=True,
    )


def _create_eve_dietary(
    state: BootstrapState, preferences: dict[str, DietaryPreference], food_items: dict[str, FoodItem]
) -> None:
    """Eve (org_beta_staff) - Pescatarian with shellfish allergy."""
    UserDietaryPreference.objects.create(
        user=state.users["org_beta_staff"],
        preference=preferences["pescatarian"],
        comment="Eat fish but no other meat",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=state.users["org_beta_staff"],
        food_item=food_items["shellfish"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Allergic to all crustaceans and mollusks",
        is_public=True,
    )


def _create_frank_dietary(state: BootstrapState, preferences: dict[str, DietaryPreference]) -> None:
    """Frank (org_beta_member) - Halal."""
    UserDietaryPreference.objects.create(
        user=state.users["org_beta_member"],
        preference=preferences["halal"],
        comment="Halal meat only, no pork or alcohol",
        is_public=True,
    )


def _create_george_dietary(state: BootstrapState, food_items: dict[str, FoodItem]) -> None:
    """George (attendee_1) - Tree nut allergy."""
    DietaryRestriction.objects.create(
        user=state.users["attendee_1"],
        food_item=food_items["tree_nuts"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Allergic to almonds, walnuts, cashews - all tree nuts",
        is_public=True,
    )


def _create_hannah_dietary(
    state: BootstrapState, preferences: dict[str, DietaryPreference], food_items: dict[str, FoodItem]
) -> None:
    """Hannah (attendee_2) - Dairy-Free and egg allergy."""
    UserDietaryPreference.objects.create(
        user=state.users["attendee_2"],
        preference=preferences["dairy_free"],
        comment="Dairy-free diet",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=state.users["attendee_2"],
        food_item=food_items["eggs"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Allergic reaction to eggs",
        is_public=True,
    )


def _create_ivan_dietary(state: BootstrapState, food_items: dict[str, FoodItem]) -> None:
    """Ivan (attendee_3) - Soy intolerance."""
    DietaryRestriction.objects.create(
        user=state.users["attendee_3"],
        food_item=food_items["soy"],
        restriction_type=DietaryRestriction.RestrictionType.INTOLERANT,
        notes="Digestive issues with soy products",
        is_public=True,
    )


def _create_julia_dietary(
    state: BootstrapState, preferences: dict[str, DietaryPreference], food_items: dict[str, FoodItem]
) -> None:
    """Julia (attendee_4) - Vegetarian and sesame allergy."""
    UserDietaryPreference.objects.create(
        user=state.users["attendee_4"],
        preference=preferences["vegetarian"],
        comment="Vegetarian, okay with dairy and eggs",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=state.users["attendee_4"],
        food_item=food_items["sesame"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Allergic to sesame seeds and tahini",
        is_public=True,
    )


def _create_karen_dietary(state: BootstrapState, preferences: dict[str, DietaryPreference]) -> None:
    """Karen (multi_org_user) - Kosher."""
    UserDietaryPreference.objects.create(
        user=state.users["multi_org_user"],
        preference=preferences["kosher"],
        comment="Keep kosher, need separate meat and dairy",
        is_public=True,
    )


def _create_leo_dietary(state: BootstrapState, food_items: dict[str, FoodItem]) -> None:
    """Leo (pending_user) - Celery allergy."""
    DietaryRestriction.objects.create(
        user=state.users["pending_user"],
        food_item=food_items["celery"],
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Allergic to celery and celeriac",
        is_public=True,
    )


def _create_maria_dietary(state: BootstrapState, preferences: dict[str, DietaryPreference]) -> None:
    """Maria (invited_user) - Pescatarian and gluten-free."""
    UserDietaryPreference.objects.create(
        user=state.users["invited_user"],
        preference=preferences["pescatarian"],
        comment="Pescatarian lifestyle",
        is_public=True,
    )
    UserDietaryPreference.objects.create(
        user=state.users["invited_user"],
        preference=preferences["gluten_free"],
        comment="Gluten sensitivity, not celiac",
        is_public=True,
    )
