"""Integration tests for the DietaryController."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, RevelUser, UserDietaryPreference

pytestmark = pytest.mark.django_db


# Food Items Tests


def test_list_food_items_returns_seeded_items(auth_client: Client) -> None:
    """Test listing food items returns seeded items from migration.

    Migration 0007_seed_common_food_items seeds ~59 food items (20 allergens x 3 languages,
    with some duplicates like "Gluten" being the same in English/German).
    The endpoint returns at most 20 items, so we verify seeded items are accessible.
    """
    url = reverse("api:list-food-items")
    response = auth_client.get(url)

    assert response.status_code == 200
    data = response.json()
    # Endpoint has a 20-item limit
    assert len(data) == 20
    # Verify database has seeded items (even if not all in first page)
    assert FoodItem.objects.filter(name="Peanuts").exists()
    assert FoodItem.objects.filter(name="Erdnüsse").exists()  # German
    assert FoodItem.objects.filter(name="Arachidi").exists()  # Italian
    # Total count should be at least 59 (some translations are the same word)
    assert FoodItem.objects.count() >= 59


def test_list_food_items_with_search(auth_client: Client) -> None:
    """Test searching for food items by name."""
    # Create unique items that won't conflict with seeded data
    FoodItem.objects.create(name="Test Peanut Butter")
    FoodItem.objects.create(name="Almond Butter")
    FoodItem.objects.create(name="Cashew Butter")

    url = reverse("api:list-food-items")
    response = auth_client.get(url, {"search": "butter"})

    assert response.status_code == 200
    data = response.json()
    # All returned items should contain "butter"
    assert len(data) >= 3
    assert all("butter" in item["name"].lower() for item in data)


def test_list_food_items_limit_20(auth_client: Client) -> None:
    """Test that list_food_items returns at most 20 items."""
    for i in range(25):
        FoodItem.objects.create(name=f"Food{i}")

    url = reverse("api:list-food-items")
    response = auth_client.get(url)

    assert response.status_code == 200
    assert len(response.json()) == 20


def test_create_food_item_success(auth_client: Client) -> None:
    """Test creating a new food item."""
    url = reverse("api:create-food-item")
    payload = {"name": "Test Unique Food Item"}

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Unique Food Item"
    assert FoodItem.objects.filter(name="Test Unique Food Item").exists()


def test_create_food_item_returns_existing(auth_client: Client) -> None:
    """Test that creating a duplicate food item returns the existing one.

    Uses seeded "Peanuts" item from migration 0007_seed_common_food_items.
    """
    # Get the existing seeded food item
    existing = FoodItem.objects.get(name="Peanuts")
    initial_count = FoodItem.objects.count()

    url = reverse("api:create-food-item")
    payload = {"name": "peanuts"}  # Different case

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(existing.id)
    # Ensure no new items were created
    assert FoodItem.objects.count() == initial_count


def test_create_food_item_requires_auth(client: Client) -> None:
    """Test that creating a food item requires authentication."""
    url = reverse("api:create-food-item")
    payload = {"name": "Peanuts"}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 401


# Dietary Restrictions Tests


def test_list_dietary_restrictions_empty(auth_client: Client) -> None:
    """Test listing dietary restrictions when none exist."""
    url = reverse("api:list-dietary-restrictions")
    response = auth_client.get(url)

    assert response.status_code == 200
    assert response.json() == []


def test_list_dietary_restrictions(auth_client: Client, user: RevelUser) -> None:
    """Test listing user's dietary restrictions."""
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=user,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Carry EpiPen",
        is_public=True,
    )

    url = reverse("api:list-dietary-restrictions")
    response = auth_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["food_item"]["name"] == "Peanuts"
    assert data[0]["restriction_type"] == "allergy"
    assert data[0]["notes"] == "Carry EpiPen"
    assert data[0]["is_public"] is True


def test_create_dietary_restriction_success(auth_client: Client, user: RevelUser) -> None:
    """Test creating a dietary restriction."""
    url = reverse("api:create-dietary-restriction")
    payload = {
        "food_item_name": "Peanuts",
        "restriction_type": "severe_allergy",
        "notes": "Carry EpiPen",
        "is_public": False,
    }

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["food_item"]["name"] == "Peanuts"
    assert data["restriction_type"] == "severe_allergy"
    assert data["notes"] == "Carry EpiPen"
    assert data["is_public"] is False

    # Verify food item was created
    assert FoodItem.objects.filter(name="Peanuts").exists()
    # Verify restriction was created
    assert DietaryRestriction.objects.filter(
        user=user, restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY
    ).exists()


def test_create_dietary_restriction_creates_food_item(auth_client: Client, user: RevelUser) -> None:
    """Test that creating a restriction auto-creates the food item."""
    unique_food = "Test Unique Allergen Item"
    assert not FoodItem.objects.filter(name=unique_food).exists()

    url = reverse("api:create-dietary-restriction")
    payload = {
        "food_item_name": unique_food,
        "restriction_type": "allergy",
        "notes": "",
        "is_public": False,
    }

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    assert FoodItem.objects.filter(name=unique_food).exists()


def test_create_dietary_restriction_uses_existing_food_item(auth_client: Client, user: RevelUser) -> None:
    """Test that creating a restriction uses an existing food item.

    Uses seeded "Peanuts" from migration 0007_seed_common_food_items.
    """
    existing_food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    initial_count = FoodItem.objects.count()

    url = reverse("api:create-dietary-restriction")
    payload = {
        "food_item_name": "peanuts",  # Different case
        "restriction_type": "allergy",
        "notes": "",
        "is_public": False,
    }

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    # No duplicate created - count stays the same
    assert FoodItem.objects.count() == initial_count
    restriction = DietaryRestriction.objects.get(user=user)
    assert restriction.food_item.id == existing_food.id


def test_create_dietary_restriction_duplicate_returns_400(auth_client: Client, user: RevelUser) -> None:
    """Test that creating a duplicate restriction returns 400."""
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=user, food_item=food, restriction_type=DietaryRestriction.RestrictionType.ALLERGY
    )

    url = reverse("api:create-dietary-restriction")
    payload = {
        "food_item_name": "Peanuts",
        "restriction_type": "severe_allergy",
        "notes": "",
        "is_public": False,
    }

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "already have a restriction" in response.json()["message"]


def test_update_dietary_restriction_success(auth_client: Client, user: RevelUser) -> None:
    """Test updating a dietary restriction."""
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    restriction = DietaryRestriction.objects.create(
        user=user,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="",
        is_public=False,
    )

    url = reverse("api:update-dietary-restriction", args=[restriction.id])
    payload = {
        "restriction_type": "severe_allergy",
        "notes": "Carry EpiPen",
        "is_public": True,
    }

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    restriction.refresh_from_db()
    assert restriction.restriction_type == "severe_allergy"
    assert restriction.notes == "Carry EpiPen"
    assert restriction.is_public is True


def test_update_dietary_restriction_partial(auth_client: Client, user: RevelUser) -> None:
    """Test partially updating a dietary restriction."""
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    restriction = DietaryRestriction.objects.create(
        user=user,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Original note",
        is_public=False,
    )

    url = reverse("api:update-dietary-restriction", args=[restriction.id])
    payload = {"is_public": True}

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    restriction.refresh_from_db()
    assert restriction.restriction_type == "allergy"  # Unchanged
    assert restriction.notes == "Original note"  # Unchanged
    assert restriction.is_public is True  # Changed


def test_update_dietary_restriction_not_found(auth_client: Client) -> None:
    """Test updating a non-existent restriction returns 404."""
    url = reverse("api:update-dietary-restriction", args=["00000000-0000-0000-0000-000000000000"])
    payload = {"is_public": True}

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_update_dietary_restriction_other_user(auth_client: Client, user: RevelUser) -> None:
    """Test that a user cannot update another user's restriction."""
    other_user = RevelUser.objects.create_user(username="other@example.com", email="other@example.com")
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    restriction = DietaryRestriction.objects.create(
        user=other_user, food_item=food, restriction_type=DietaryRestriction.RestrictionType.ALLERGY
    )

    url = reverse("api:update-dietary-restriction", args=[restriction.id])
    payload = {"is_public": True}

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404  # get_object_or_404 returns 404 for wrong user


def test_delete_dietary_restriction_success(auth_client: Client, user: RevelUser) -> None:
    """Test deleting a dietary restriction."""
    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    restriction = DietaryRestriction.objects.create(
        user=user, food_item=food, restriction_type=DietaryRestriction.RestrictionType.ALLERGY
    )

    url = reverse("api:delete-dietary-restriction", args=[restriction.id])
    response = auth_client.delete(url)

    assert response.status_code == 204
    assert not DietaryRestriction.objects.filter(id=restriction.id).exists()


def test_delete_dietary_restriction_not_found(auth_client: Client) -> None:
    """Test deleting a non-existent restriction returns 404."""
    url = reverse("api:delete-dietary-restriction", args=["00000000-0000-0000-0000-000000000000"])
    response = auth_client.delete(url)

    assert response.status_code == 404


# Dietary Preferences Tests


def test_list_dietary_preferences(auth_client: Client) -> None:
    """Test listing all available dietary preferences.

    DietaryPreferences are seeded via migration (0006_seed_dietary_preferences.py).
    This test verifies that seeded preferences are accessible via the API.
    """
    url = reverse("api:list-dietary-preferences")
    response = auth_client.get(url)

    assert response.status_code == 200
    data = response.json()
    # Should have at least the 10 seeded preferences
    assert len(data) >= 10
    preference_names = {p["name"] for p in data}
    # Verify some of the seeded preferences exist
    assert "Vegetarian" in preference_names
    assert "Vegan" in preference_names


def test_list_my_dietary_preferences_empty(auth_client: Client) -> None:
    """Test listing user's preferences when none are selected."""
    url = reverse("api:list-my-dietary-preferences")
    response = auth_client.get(url)

    assert response.status_code == 200
    assert response.json() == []


def test_list_my_dietary_preferences(auth_client: Client, user: RevelUser) -> None:
    """Test listing user's selected dietary preferences.

    Uses a seeded DietaryPreference (Vegetarian) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegetarian")
    UserDietaryPreference.objects.create(user=user, preference=pref, comment="Strictly vegetarian", is_public=True)

    url = reverse("api:list-my-dietary-preferences")
    response = auth_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["preference"]["name"] == "Vegetarian"
    assert data[0]["comment"] == "Strictly vegetarian"
    assert data[0]["is_public"] is True


def test_add_dietary_preference_success(auth_client: Client, user: RevelUser) -> None:
    """Test adding a dietary preference to user profile.

    Uses a seeded DietaryPreference (Vegan) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegan")

    url = reverse("api:add-dietary-preference")
    payload = {"preference_id": str(pref.id), "comment": "Strict vegan", "is_public": True}

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["preference"]["name"] == "Vegan"
    assert data["comment"] == "Strict vegan"
    assert data["is_public"] is True
    assert UserDietaryPreference.objects.filter(user=user, preference=pref).exists()


def test_add_dietary_preference_not_found(auth_client: Client) -> None:
    """Test adding a non-existent preference returns 422 for invalid UUID.

    The UUID validation fails before the database lookup, resulting in a 422 validation error
    instead of a 404 not found error.
    """
    url = reverse("api:add-dietary-preference")
    payload = {"preference_id": "invalid-uuid", "comment": "", "is_public": False}

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422


def test_add_dietary_preference_valid_uuid_not_found(auth_client: Client) -> None:
    """Test adding a non-existent preference with valid UUID returns 404.

    When a valid v4 UUID is provided but no matching preference exists, the controller
    returns a 404 not found error.
    """
    url = reverse("api:add-dietary-preference")
    # Use a valid v4 UUID that doesn't exist in the database
    payload = {"preference_id": "12345678-1234-4234-a234-123456789012", "comment": "", "is_public": False}

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_add_dietary_preference_duplicate_returns_400(auth_client: Client, user: RevelUser) -> None:
    """Test adding a duplicate preference returns 400.

    Uses a seeded DietaryPreference (Vegan) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegan")
    UserDietaryPreference.objects.create(user=user, preference=pref)

    url = reverse("api:add-dietary-preference")
    payload = {"preference_id": str(pref.id), "comment": "", "is_public": False}

    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "already added" in response.json()["message"]


def test_update_dietary_preference_success(auth_client: Client, user: RevelUser) -> None:
    """Test updating a dietary preference.

    Uses a seeded DietaryPreference (Vegan) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegan")
    user_pref = UserDietaryPreference.objects.create(user=user, preference=pref, comment="", is_public=False)

    url = reverse("api:update-dietary-preference", args=[user_pref.id])
    payload = {"comment": "Strictly vegan", "is_public": True}

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    user_pref.refresh_from_db()
    assert user_pref.comment == "Strictly vegan"
    assert user_pref.is_public is True


def test_update_dietary_preference_partial(auth_client: Client, user: RevelUser) -> None:
    """Test partially updating a dietary preference.

    Uses a seeded DietaryPreference (Vegan) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegan")
    user_pref = UserDietaryPreference.objects.create(user=user, preference=pref, comment="Original", is_public=False)

    url = reverse("api:update-dietary-preference", args=[user_pref.id])
    payload = {"is_public": True}

    response = auth_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    user_pref.refresh_from_db()
    assert user_pref.comment == "Original"  # Unchanged
    assert user_pref.is_public is True


def test_delete_dietary_preference_success(auth_client: Client, user: RevelUser) -> None:
    """Test deleting a dietary preference.

    Uses a seeded DietaryPreference (Vegan) instead of creating a new one.
    """
    # Use an existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegan")
    user_pref = UserDietaryPreference.objects.create(user=user, preference=pref)

    url = reverse("api:delete-dietary-preference", args=[user_pref.id])
    response = auth_client.delete(url)

    assert response.status_code == 204
    assert not UserDietaryPreference.objects.filter(id=user_pref.id).exists()


def test_delete_dietary_preference_not_found(auth_client: Client) -> None:
    """Test deleting a non-existent preference returns 404."""
    url = reverse("api:delete-dietary-preference", args=["00000000-0000-0000-0000-000000000000"])
    response = auth_client.delete(url)

    assert response.status_code == 404
