import typing as t

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

from accounts.models import FoodItem, RevelUser
from moderation.models import ContentReport


@pytest.mark.django_db
def test_content_report_targets_any_model(user: RevelUser) -> None:
    food = FoodItem.objects.create(name="peanut")
    report = ContentReport.objects.create(
        content_type=ContentType.objects.get_for_model(FoodItem),
        object_id=food.id,
        reporter=user,
        reason=ContentReport.Reason.OFFENSIVE,
        content_snapshot=food.name,
    )
    assert report.content_object == food
    assert report.status == ContentReport.Status.OPEN
    assert report.source == ContentReport.Source.USER_REPORT


@pytest.mark.django_db
def test_one_open_report_per_reporter_per_object(user: RevelUser) -> None:
    """TimeStampedModel.save() calls full_clean(), which surfaces the conditional
    UniqueConstraint as ValidationError before any DB-level IntegrityError can fire."""
    food = FoodItem.objects.create(name="peanut")
    ct = ContentType.objects.get_for_model(FoodItem)
    common: dict[str, t.Any] = dict(content_type=ct, object_id=food.id, reporter=user,
                                    reason=ContentReport.Reason.OFFENSIVE)
    ContentReport.objects.create(**common)
    with pytest.raises(ValidationError):
        ContentReport.objects.create(**common)
