"""Machine-readable ``code`` / ``forceable`` on refused membership-application approvals."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(organization_owner_user).access_token}"  # type: ignore[attr-defined]
    return client


@pytest.fixture
def applicant(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="applicant@example.com", email="applicant@example.com", password="pw", email_verified=True
    )


def _application(organization: Organization, user: RevelUser, tier: MembershipTier, **extra: t.Any) -> t.Any:
    return OrganizationMembershipRequest.objects.create(organization=organization, user=user, tier=tier, **extra)


def _record(organization: Organization, **extra: t.Any) -> MembershipRecord:
    extra.setdefault("valid_from", timezone.now() - timedelta(days=1))
    return MembershipRecord.objects.create(
        organization=organization, full_name="Entry", source=MembershipRecord.Source.MANUAL, **extra
    )


def _approve(client: Client, organization: Organization, application: t.Any, **body: t.Any) -> t.Any:
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": application.pk})
    return client.post(url, data=body, content_type="application/json")


def _assert_refused(response: t.Any, code: str, forceable: bool) -> None:
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["code"] == code
    assert body["forceable"] is forceable
    assert isinstance(body["detail"], str) and body["detail"]


class TestForceableRefusals:
    def test_active_subscription_is_forceable(
        self,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
    ) -> None:
        plan = MembershipSubscriptionPlan.objects.create(
            tier=tier, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
        )
        MembershipSubscription.objects.create(
            user=applicant,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        application = _application(organization, applicant, tier)

        _assert_refused(_approve(owner_client, organization, application), "subscription_active", True)

        assert _approve(owner_client, organization, application, force=True).status_code == 204
        application.refresh_from_db()
        assert application.status == OrganizationMembershipRequest.Status.COMPLETED

    def test_paused_membership_is_forceable(
        self,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=applicant,
            tier=tier,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )
        application = _application(organization, applicant, tier)

        _assert_refused(_approve(owner_client, organization, application), "membership_paused", True)

        assert _approve(owner_client, organization, application, force=True).status_code == 204


class TestNonForceableRefusals:
    def test_paid_application_cannot_be_linked(
        self,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
    ) -> None:
        plan = MembershipSubscriptionPlan.objects.create(
            tier=tier, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
        )
        application = _application(organization, applicant, tier, plan=plan)
        record = _record(organization)

        for force in (False, True):
            response = _approve(owner_client, organization, application, link_record_id=str(record.pk), force=force)
            _assert_refused(response, "link_paid_application", False)

    def test_future_dated_entry(
        self,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
    ) -> None:
        record = _record(organization, valid_from=timezone.now() + timedelta(days=3))
        application = _application(organization, applicant, tier)

        response = _approve(owner_client, organization, application, link_record_id=str(record.pk), force=True)

        _assert_refused(response, "link_future_dated", False)

    @pytest.mark.parametrize("kind", ["linked", "merged", "closed", "non_standard"])
    def test_unavailable_entry(
        self,
        kind: str,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        survivor = _record(organization)
        other = django_user_model.objects.create_user(username="o@example.com", email="o@example.com", password="pw")
        extras: dict[str, dict[str, t.Any]] = {
            "linked": {"user": other},
            "merged": {"merged_into": survivor},
            "closed": {"valid_until": timezone.now() + timedelta(hours=1)},
            "non_standard": {"kind": next(k for k in MembershipRecord.Kind if k != MembershipRecord.Kind.STANDARD)},
        }
        record = _record(organization, **extras[kind])
        application = _application(organization, applicant, tier)

        response = _approve(owner_client, organization, application, link_record_id=str(record.pk), force=True)

        _assert_refused(response, "link_unavailable", False)

    def test_applicant_already_has_open_entry(
        self,
        owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        applicant: RevelUser,
    ) -> None:
        _record(organization, user=applicant)
        record = _record(organization)
        application = _application(organization, applicant, tier)

        response = _approve(owner_client, organization, application, link_record_id=str(record.pk), force=True)

        _assert_refused(response, "applicant_has_entry", False)

    def test_plain_400_keeps_error_detail_shape(
        self,
        owner_client: Client,
        organization: Organization,
        applicant: RevelUser,
    ) -> None:
        application = OrganizationMembershipRequest.objects.create(organization=organization, user=applicant)

        response = _approve(owner_client, organization, application)

        assert response.status_code == 400
        assert "code" not in response.json()
