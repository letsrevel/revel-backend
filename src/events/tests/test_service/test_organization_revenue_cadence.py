"""Cadence field + enable validation (#552)."""

import typing as t

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.exceptions import RevenueReportCadenceOwnerOnlyError
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
        organization_service.update_organization(org, payload, requester=owner)


@pytest.mark.django_db
def test_enabling_cadence_with_billing_email_succeeds(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner, billing_email="b@example.com")
    payload = OrganizationEditSchema(
        visibility=Organization.Visibility.STAFF_ONLY,
        revenue_report_cadence=Organization.RevenueReportCadence.MONTHLY,
    )
    updated = organization_service.update_organization(org, payload, requester=owner)
    assert updated.revenue_report_cadence == Organization.RevenueReportCadence.MONTHLY


@pytest.mark.django_db
def test_non_owner_cannot_change_cadence(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    staff = RevelUser.objects.create_user(username="s", email="s@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner, billing_email="b@example.com")
    payload = OrganizationEditSchema(
        visibility=Organization.Visibility.STAFF_ONLY,
        revenue_report_cadence=Organization.RevenueReportCadence.MONTHLY,
    )
    with pytest.raises(RevenueReportCadenceOwnerOnlyError):
        organization_service.update_organization(org, payload, requester=staff)
    org.refresh_from_db()
    assert org.revenue_report_cadence == Organization.RevenueReportCadence.NONE


@pytest.mark.django_db
def test_non_owner_noop_cadence_is_allowed(db: t.Any) -> None:
    """A non-owner submitting the unchanged cadence (e.g. echoing the form) is not blocked."""
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    staff = RevelUser.objects.create_user(username="s", email="s@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner)
    payload = OrganizationEditSchema(
        visibility=Organization.Visibility.PUBLIC,
        revenue_report_cadence=Organization.RevenueReportCadence.NONE,
    )
    updated = organization_service.update_organization(org, payload, requester=staff)
    assert updated.visibility == Organization.Visibility.PUBLIC
    assert updated.revenue_report_cadence == Organization.RevenueReportCadence.NONE
