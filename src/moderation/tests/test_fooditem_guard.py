"""Integration tests for the food-item name guard (block / allow)."""

import pytest
from django.test import Client

from accounts.models import DietaryRestriction, FoodItem
from moderation.blocklist import screen as screen_mod


@pytest.fixture(autouse=True)
def _synthetic_wordlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the blocklist loader with a one-word synthetic wordlist.

    Patching the name in screen.py's module namespace bypasses lru_cache on the
    original loader and is picked up at call time by is_blocked().
    """
    monkeypatch.setattr(screen_mod, "load_blocklist", lambda: frozenset({"badword"}))


@pytest.mark.django_db
def test_create_food_item_blocks_exact(auth_client: Client) -> None:
    """Exact blocklist match → 422, no FoodItem row created."""
    resp = auth_client.post(
        "/api/dietary/food-items",
        data={"name": "badword"},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert not FoodItem.objects.filter(name__iexact="badword").exists()


@pytest.mark.django_db
def test_create_dietary_restriction_blocks_exact(auth_client: Client) -> None:
    """The guard is wired into the restriction path too: blocked name → 422, no rows created."""
    resp = auth_client.post(
        "/api/dietary/restrictions",
        data={"food_item_name": "badword", "restriction_type": "allergy"},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert not FoodItem.objects.filter(name__iexact="badword").exists()
    assert not DietaryRestriction.objects.exists()


@pytest.mark.django_db
def test_create_food_item_allows_benign(auth_client: Client) -> None:
    """Benign name → created normally."""
    resp = auth_client.post(
        "/api/dietary/food-items",
        data={"name": "peanut"},
        content_type="application/json",
    )
    assert resp.status_code in (200, 201)
    assert FoodItem.objects.filter(name__iexact="peanut").exists()
