"""Integration tests for the food-item name guard (block / escalate / allow)."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from accounts.models import DietaryRestriction, FoodItem, RevelUser
from moderation.blocklist import screen as screen_mod
from moderation.models import ContentReport
from moderation.service.fooditem_guard import screen_food_item_name


@pytest.fixture(autouse=True)
def _synthetic_wordlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the blocklist loader with a one-word synthetic wordlist.

    Patching the name in screen.py's module namespace bypasses lru_cache on the
    original loader and is picked up at call-time by screen().
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
def test_create_food_item_escalates_near_miss(auth_client: Client, user: RevelUser) -> None:
    """Near-miss (fuzzy >= 80) → 200/201 + a BLOCKLIST ContentReport is filed."""
    resp = auth_client.post(
        "/api/dietary/food-items",
        data={"name": "badwrd"},
        content_type="application/json",
    )
    assert resp.status_code in (200, 201)
    food = FoodItem.objects.get(name__iexact="badwrd")
    assert ContentReport.objects.filter(
        object_id=food.id,
        source=ContentReport.Source.BLOCKLIST,
    ).exists()


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
    """Benign name → 200/201, no ContentReport created."""
    resp = auth_client.post(
        "/api/dietary/food-items",
        data={"name": "peanut"},
        content_type="application/json",
    )
    assert resp.status_code in (200, 201)
    assert not ContentReport.objects.exists()


@pytest.mark.django_db
def test_escalate_does_not_collide_with_existing_user_report(user: RevelUser) -> None:
    """Regression: ESCALATE must not 500 when the user already has an open USER_REPORT on the item.

    A blocklist escalation is a SYSTEM signal (reporter=None). The partial unique index on
    (content_type, object_id, reporter) WHERE status='open' only collides on identical reporters,
    so reporter=None and reporter=<user> coexist safely.
    """
    food_item = FoodItem.objects.create(name="badwrd")
    ct = ContentType.objects.get_for_model(FoodItem)
    # Seed an existing open USER_REPORT from this user on the same item.
    ContentReport.objects.create(
        content_type=ct,
        object_id=food_item.pk,
        reporter=user,
        source=ContentReport.Source.USER_REPORT,
        status=ContentReport.Status.OPEN,
        reason=ContentReport.Reason.OFFENSIVE,
        content_snapshot=food_item.name,
    )

    # Calling screen_food_item_name with the near-miss name (triggers ESCALATE) must not raise.
    screen_food_item_name(food_item.name, food_item=food_item)

    # A BLOCKLIST report with reporter=None should now exist alongside the USER_REPORT.
    assert ContentReport.objects.filter(
        object_id=food_item.pk,
        source=ContentReport.Source.BLOCKLIST,
        reporter=None,
        status=ContentReport.Status.OPEN,
    ).exists()
    # The original user report is untouched.
    assert ContentReport.objects.filter(
        object_id=food_item.pk,
        source=ContentReport.Source.USER_REPORT,
        reporter=user,
    ).exists()
