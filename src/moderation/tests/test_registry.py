import uuid

import pytest
from django.http import Http404

from accounts.models import FoodItem
from moderation.registry import resolve_reportable


@pytest.mark.django_db
def test_resolves_reportable_model_and_snapshot() -> None:
    food = FoodItem.objects.create(name="peanut")
    instance, snapshot = resolve_reportable("accounts.fooditem", food.id)
    assert instance == food
    assert snapshot == "peanut"


@pytest.mark.django_db
def test_non_reportable_model_404() -> None:
    with pytest.raises(Http404):
        resolve_reportable("accounts.globalban", uuid.uuid4())


@pytest.mark.django_db
def test_missing_object_404() -> None:
    with pytest.raises(Http404):
        resolve_reportable("accounts.fooditem", uuid.uuid4())
