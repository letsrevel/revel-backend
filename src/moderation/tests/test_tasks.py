import pytest

from accounts.models import FoodItem
from moderation.models import ContentReport
from moderation.tasks import sweep_food_items_for_blocklist


@pytest.fixture(autouse=True)
def _synthetic_wordlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("moderation.blocklist.screen.load_blocklist", lambda: frozenset({"badword"}))


@pytest.mark.django_db
def test_sweep_escalates_existing_offender() -> None:
    FoodItem.objects.create(name="badword")
    FoodItem.objects.create(name="peanut")
    result = sweep_food_items_for_blocklist()
    assert result["escalated"] == 1
    assert ContentReport.objects.filter(source=ContentReport.Source.BLOCKLIST).count() == 1


@pytest.mark.django_db
def test_sweep_is_idempotent() -> None:
    FoodItem.objects.create(name="badword")
    sweep_food_items_for_blocklist()
    sweep_food_items_for_blocklist()
    assert ContentReport.objects.filter(source=ContentReport.Source.BLOCKLIST).count() == 1
