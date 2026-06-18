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
    common: dict[str, t.Any] = {
        "content_type": ct,
        "object_id": food.id,
        "reporter": user,
        "reason": ContentReport.Reason.OFFENSIVE,
    }
    ContentReport.objects.create(**common)
    with pytest.raises(ValidationError) as exc_info:
        ContentReport.objects.create(**common)
    assert "unique_open_report_per_reporter_per_object" in str(exc_info.value)


@pytest.mark.django_db
def test_partial_uniqueness_allows_second_report_when_first_is_dismissed(user: RevelUser) -> None:
    """The UniqueConstraint is PARTIAL (condition=Q(status='open')), so a second report
    for the same (content_type, object_id, reporter) must succeed once the first is DISMISSED."""
    food = FoodItem.objects.create(name="banana")
    ct = ContentType.objects.get_for_model(FoodItem)
    common: dict[str, t.Any] = {
        "content_type": ct,
        "object_id": food.id,
        "reporter": user,
        "reason": ContentReport.Reason.OFFENSIVE,
    }
    first = ContentReport.objects.create(**common)
    first.status = ContentReport.Status.DISMISSED
    first.save(update_fields=["status"])
    # Second report for the same target should succeed because first is no longer open.
    ContentReport.objects.create(**common)
    assert ContentReport.objects.filter(content_type=ct, object_id=food.id, reporter=user).count() == 2
