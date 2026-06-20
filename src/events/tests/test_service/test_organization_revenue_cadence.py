"""Cadence field + enable validation (#552)."""

import typing as t

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import Organization
from events.schema import OrganizationEditSchema
from events.service import organization_service


@pytest.mark.django_db
def test_enabling_cadence_without_billing_email_is_rejected(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner)  # no billing_email
    payload = OrganizationEditSchema(
        visibility=Organization.Visibility.STAFF_ONLY,
        revenue_report_cadence=Organization.RevenueReportCadence.QUARTERLY,
    )
    with pytest.raises(ValidationError):
        organization_service.update_organization(org, payload)


@pytest.mark.django_db
def test_enabling_cadence_with_billing_email_succeeds(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner, billing_email="b@example.com")
    payload = OrganizationEditSchema(
        visibility=Organization.Visibility.STAFF_ONLY,
        revenue_report_cadence=Organization.RevenueReportCadence.MONTHLY,
    )
    updated = organization_service.update_organization(org, payload)
    assert updated.revenue_report_cadence == Organization.RevenueReportCadence.MONTHLY
