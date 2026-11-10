"""Org-admin lookup of an account by exact verified email: privacy contract, shape, permissions, throttle."""

import datetime as dt
import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    MembershipRecord,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
)

pytestmark = pytest.mark.django_db

EMAIL = "Lookup.Target@Example.com"
NOT_FOUND = {"detail": "User not found.", "code": "user_not_found"}


def _url(organization: Organization) -> str:
    return reverse("api:lookup_organization_user", kwargs={"slug": organization.slug})


@pytest.fixture
def target(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(email=EMAIL, username=EMAIL, email_verified=True)


def _record(organization: Organization, user: RevelUser, **kwargs: t.Any) -> MembershipRecord:
    return MembershipRecord.objects.create(
        organization=organization,
        user=user,
        full_name="Some Person",
        valid_from=timezone.now() - dt.timedelta(days=30),
        source=MembershipRecord.Source.MANUAL,
        **kwargs,
    )


def _lookup(client: Client, organization: Organization, email: str = EMAIL) -> t.Any:
    return client.get(_url(organization), {"email": email})


class TestMatching:
    def test_found_returns_minimal_body(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        response = _lookup(organization_owner_client, organization)

        assert response.status_code == 200
        assert response.json() == {
            "user_id": str(target.pk),
            "display_name": target.preferred_name,
            "is_member": False,
            "membership_status": None,
            "register_record_id": None,
            "pending_request_id": None,
        }
        assert EMAIL.lower() not in response.content.decode().lower()

    def test_case_insensitive(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _lookup(organization_owner_client, organization, EMAIL.upper()).status_code == 200
        assert _lookup(organization_owner_client, organization, EMAIL.lower()).json()["user_id"] == str(target.pk)

    def test_no_partial_match(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        for probe in ("lookup.target@example.co", "ookup.target@example.com", "lookup.target@example.com.au"):
            assert _lookup(organization_owner_client, organization, probe).status_code == 404

    @pytest.mark.parametrize(
        "changes",
        [{"email_verified": False}, {"guest": True}, {"is_active": False}, None],
        ids=["unverified", "guest", "inactive", "nonexistent"],
    )
    def test_every_non_match_is_the_same_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        target: RevelUser,
        changes: dict[str, bool] | None,
    ) -> None:
        email = EMAIL
        if changes is None:
            email = "nobody@example.com"
        else:
            RevelUser.objects.filter(pk=target.pk).update(**changes)

        response = _lookup(organization_owner_client, organization, email)

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_ambiguous_email_is_a_non_match(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        # ``email`` is not unique at the DB level; never pick one of two accounts arbitrarily.
        RevelUser.objects.create_user(username="dup", email=EMAIL.upper(), password="x", email_verified=True)

        response = _lookup(organization_owner_client, organization)

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_invalid_email_is_422(self, organization_owner_client: Client, organization: Organization) -> None:
        assert _lookup(organization_owner_client, organization, "not-an-email").status_code == 422
        assert organization_owner_client.get(_url(organization)).status_code == 422


class TestStanding:
    @pytest.mark.parametrize("status", OrganizationMember.MembershipStatus.values)
    def test_membership_status(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser, status: str
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=target, status=status)

        body = _lookup(organization_owner_client, organization).json()

        assert body["membership_status"] == status
        assert body["is_member"] is (status == OrganizationMember.MembershipStatus.ACTIVE)

    def test_open_standard_register_record(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        _record(organization, target, kind=MembershipRecord.Kind.TEMPORARY)
        ended = _record(
            organization,
            target,
            kind=MembershipRecord.Kind.STANDARD,
            valid_until=timezone.now() - dt.timedelta(days=1),
        )
        other_org = Organization.objects.create(name="Other", slug="other", owner=organization.owner)
        _record(other_org, target, kind=MembershipRecord.Kind.STANDARD)
        assert _lookup(organization_owner_client, organization).json()["register_record_id"] is None

        record = _record(organization, target, kind=MembershipRecord.Kind.STANDARD)

        assert record.pk != ended.pk
        assert _lookup(organization_owner_client, organization).json()["register_record_id"] == str(record.pk)

    def test_pending_request_only(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        OrganizationMembershipRequest.objects.create(
            organization=organization, user=target, status=OrganizationMembershipRequest.Status.REJECTED
        )
        assert _lookup(organization_owner_client, organization).json()["pending_request_id"] is None

        pending = OrganizationMembershipRequest.objects.create(
            organization=organization, user=target, status=OrganizationMembershipRequest.Status.PENDING
        )

        assert _lookup(organization_owner_client, organization).json()["pending_request_id"] == str(pending.pk)

    def test_display_name_never_falls_back_to_the_email(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        RevelUser.objects.filter(pk=target.pk).update(preferred_name="", first_name="", last_name="")

        response = _lookup(organization_owner_client, organization)

        assert response.json()["display_name"] == ""
        assert EMAIL.lower() not in response.content.decode().lower()


class TestPermissions:
    def test_staff_with_manage_members(
        self,
        organization_staff_client: Client,
        staff_member: OrganizationStaff,
        organization: Organization,
        target: RevelUser,
    ) -> None:
        staff_member.permissions["default"]["manage_members"] = True
        staff_member.save(update_fields=["permissions"])

        assert _lookup(organization_staff_client, organization).status_code == 200

    def test_staff_without_manage_members_forbidden(
        self,
        organization_staff_client: Client,
        organization: Organization,
        target: RevelUser,
    ) -> None:
        response = _lookup(organization_staff_client, organization)

        assert response.status_code == 403
        assert set(response.json()) == {"detail"}

    def test_non_staff_forbidden(self, nonmember_client: Client, organization: Organization) -> None:
        Organization.objects.filter(pk=organization.pk).update(visibility=Organization.Visibility.PUBLIC)
        assert _lookup(nonmember_client, organization).status_code == 403

    def test_anonymous_unauthorized(self, organization: Organization) -> None:
        assert _lookup(Client(), organization).status_code == 401


class TestAuditAndThrottle:
    def test_audit_log_never_contains_the_email(
        self,
        organization_owner_client: Client,
        organization: Organization,
        target: RevelUser,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with caplog.at_level("DEBUG"):
            _lookup(organization_owner_client, organization)
            _lookup(organization_owner_client, organization, "missing.person@example.com")

        output = caplog.text + "".join(capsys.readouterr())
        assert "org_user_lookup" in output
        assert str(target.pk) in output
        assert "missing.person" not in output.lower()
        assert "lookup.target" not in output.lower()

    def test_throttled_to_30_per_hour(
        self,
        settings: t.Any,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        settings.DISABLE_THROTTLING = False
        for _ in range(30):
            assert _lookup(organization_owner_client, organization, "nobody@example.com").status_code == 404

        assert _lookup(organization_owner_client, organization, "nobody@example.com").status_code == 429
