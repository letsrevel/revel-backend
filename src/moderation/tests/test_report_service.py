import uuid

import pytest
from django.http import Http404

from accounts.models import FoodItem, RevelUser
from moderation.models import ContentReport
from moderation.schema import ReportCreateSchema
from moderation.service.report import create_content_report


@pytest.mark.django_db
def test_create_report_snapshots_text(user: RevelUser) -> None:
    food = FoodItem.objects.create(name="offensive-thing")
    payload = ReportCreateSchema(content_type="accounts.fooditem", object_id=food.id,
                                 reason=ContentReport.Reason.OFFENSIVE)
    report = create_content_report(reporter=user, payload=payload)
    assert report.content_snapshot == "offensive-thing"
    assert report.content_object == food


@pytest.mark.django_db
def test_duplicate_report_is_idempotent(user: RevelUser) -> None:
    food = FoodItem.objects.create(name="peanut")
    payload = ReportCreateSchema(content_type="accounts.fooditem", object_id=food.id,
                                 reason=ContentReport.Reason.OFFENSIVE)
    first = create_content_report(reporter=user, payload=payload)
    second = create_content_report(reporter=user, payload=payload)
    assert first.id == second.id
    assert ContentReport.objects.count() == 1


@pytest.mark.django_db
def test_distinct_reporters_both_recorded(user: RevelUser, revel_user_factory) -> None:  # type: ignore[no-untyped-def]
    other = revel_user_factory()
    food = FoodItem.objects.create(name="peanut")
    payload = ReportCreateSchema(content_type="accounts.fooditem", object_id=food.id,
                                 reason=ContentReport.Reason.OFFENSIVE)
    create_content_report(reporter=user, payload=payload)
    create_content_report(reporter=other, payload=payload)
    assert ContentReport.objects.count() == 2


@pytest.mark.django_db
def test_report_survives_target_deletion(user: RevelUser) -> None:
    food = FoodItem.objects.create(name="gone-soon")
    payload = ReportCreateSchema(content_type="accounts.fooditem", object_id=food.id,
                                 reason=ContentReport.Reason.OFFENSIVE)
    report = create_content_report(reporter=user, payload=payload)
    food.delete()
    report.refresh_from_db()
    assert report.content_snapshot == "gone-soon"


@pytest.mark.django_db
def test_non_reportable_target_404(user: RevelUser) -> None:
    payload = ReportCreateSchema(content_type="accounts.globalban", object_id=uuid.uuid4(),
                                 reason=ContentReport.Reason.OFFENSIVE)
    with pytest.raises(Http404):
        create_content_report(reporter=user, payload=payload)
