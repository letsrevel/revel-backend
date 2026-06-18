import uuid

import pytest
from django.test import Client

from accounts.models import FoodItem, RevelUser
from moderation.models import ContentReport


@pytest.mark.django_db
def test_report_endpoint_creates_report(auth_client: Client, user: RevelUser) -> None:
    food = FoodItem.objects.create(name="bad-thing")
    resp = auth_client.post(
        "/api/moderation/reports",
        data={"content_type": "accounts.fooditem", "object_id": str(food.id), "reason": "offensive"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    assert ContentReport.objects.filter(object_id=food.id, reporter=user).exists()


@pytest.mark.django_db
def test_report_endpoint_non_reportable_404(auth_client: Client) -> None:
    resp = auth_client.post(
        "/api/moderation/reports",
        data={"content_type": "accounts.globalban", "object_id": str(uuid.uuid4()), "reason": "offensive"},
        content_type="application/json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_report_endpoint_requires_auth(client: Client) -> None:
    resp = client.post(
        "/api/moderation/reports",
        data={"content_type": "accounts.fooditem", "object_id": str(uuid.uuid4()), "reason": "offensive"},
        content_type="application/json",
    )
    assert resp.status_code == 401
