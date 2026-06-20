"""Org-admin revenue report endpoints (#551)."""

import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.models import FileExport
from events.models import Organization


@pytest.fixture
def owner_org(db: t.Any) -> tuple[RevelUser, Organization]:
    owner = RevelUser.objects.create_user(username="owner", email="owner@example.com", password="x")
    org = Organization.objects.create(
        name="Org", slug="org", owner=owner, vat_rate=Decimal("20.00"), vat_country_code="AT"
    )
    return owner, org


def _jwt_client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_post_revenue_report_returns_file_export(owner_org: t.Any, django_capture_on_commit_callbacks: t.Any) -> None:
    owner, org = owner_org
    client = _jwt_client(owner)
    url = reverse("api:create_revenue_report", kwargs={"slug": org.slug})
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url, data={}, content_type="application/json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["export_type"] == FileExport.ExportType.REVENUE_VAT_REPORT
    assert FileExport.objects.filter(id=body["id"]).exists()


@pytest.mark.django_db
def test_malformed_event_id_returns_422(owner_org: t.Any) -> None:
    """A non-UUID event_id is rejected at the schema boundary (422), not a 500."""
    owner, org = owner_org
    client = _jwt_client(owner)
    url = reverse("api:create_revenue_report", kwargs={"slug": org.slug})
    resp = client.post(url, data={"event_id": "not-a-uuid"}, content_type="application/json")
    assert resp.status_code == 422


@pytest.mark.django_db
def test_inverted_date_range_returns_422(owner_org: t.Any) -> None:
    """A date_to before the defaulted date_from (e.g. a prior-year date_to only) is rejected, not silently empty."""
    owner, org = owner_org
    client = _jwt_client(owner)
    url = reverse("api:create_revenue_report", kwargs={"slug": org.slug})
    resp = client.post(url, data={"date_to": "2000-01-01"}, content_type="application/json")
    assert resp.status_code == 422


@pytest.mark.django_db
def test_non_admin_is_denied(owner_org: t.Any) -> None:
    _, org = owner_org
    stranger = RevelUser.objects.create_user(username="x", email="x@example.com", password="x")
    client = _jwt_client(stranger)
    url = reverse("api:create_revenue_report", kwargs={"slug": org.slug})
    resp = client.post(url, data={}, content_type="application/json")
    assert resp.status_code in (403, 404)


@pytest.mark.django_db
def test_get_poll_returns_status(owner_org: t.Any, django_capture_on_commit_callbacks: t.Any) -> None:
    owner, org = owner_org
    client = _jwt_client(owner)
    post_url = reverse("api:create_revenue_report", kwargs={"slug": org.slug})
    with django_capture_on_commit_callbacks(execute=True):
        created = client.post(post_url, data={}, content_type="application/json").json()
    get_url = reverse("api:get_revenue_report", kwargs={"slug": org.slug, "export_id": created["id"]})
    resp = client.get(get_url)
    assert resp.status_code == 200
    assert resp.json()["status"] in (
        FileExport.ExportStatus.READY,
        FileExport.ExportStatus.PROCESSING,
        FileExport.ExportStatus.PENDING,
    )
