"""Tests for the org-scoped membership verification endpoint (door-staff lookup)."""

import uuid

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import MembershipTier, Organization, OrganizationMember, OrganizationStaff

pytestmark = pytest.mark.django_db


def _verify_url(organization: Organization, code: str) -> str:
    return reverse("api:verify_organization_member", kwargs={"slug": organization.slug, "code": code})


@pytest.fixture
def member(organization_membership: OrganizationMember) -> OrganizationMember:
    """An ACTIVE member of ``organization`` (see root conftest ``organization_membership``)."""
    organization_membership.refresh_from_db()
    return organization_membership


# --- 1. Owner verifies member: 200, status/tier/user fields present ---


def test_verify_member_by_owner(
    organization_owner_client: Client, organization: Organization, member: OrganizationMember
) -> None:
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    member.tier = tier
    member.save(update_fields=["tier"])

    response = organization_owner_client.get(_verify_url(organization, member.qr_payload))

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["member_id"] == str(member.id)
    assert data["status"] == OrganizationMember.MembershipStatus.ACTIVE
    assert data["tier"]["id"] == str(tier.id)
    assert data["tier"]["name"] == "Gold"
    assert data["user"]["email"] == member.user.email


# --- 2. Bare member UUID (no prefix) -> 200 (tolerant) ---


def test_verify_member_bare_uuid_is_tolerated(
    organization_owner_client: Client, organization: Organization, member: OrganizationMember
) -> None:
    response = organization_owner_client.get(_verify_url(organization, str(member.id)))

    assert response.status_code == 200, response.content
    assert response.json()["member_id"] == str(member.id)


# --- 3. Staff WITH check_in_attendees (default map) -> 200 ---


def test_verify_member_by_staff_with_default_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member: OrganizationMember,
) -> None:
    response = organization_staff_client.get(_verify_url(organization, member.qr_payload))

    assert response.status_code == 200, response.content


# --- 4. Staff with check_in_attendees explicitly False -> 403 ---


def test_verify_member_by_staff_without_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member: OrganizationMember,
) -> None:
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = False
    staff_member.permissions = perms
    staff_member.save()

    response = organization_staff_client.get(_verify_url(organization, member.qr_payload))

    assert response.status_code == 403


# --- 5. Plain member (non-staff) -> 403; unauthenticated -> 401 ---


def test_verify_member_by_plain_member_forbidden(
    member_client: Client, organization: Organization, nonmember_user: RevelUser
) -> None:
    # ``member_client`` fixture already makes its own user a member of ``organization``;
    # the verification target here is a distinct member so the two creates don't collide.
    target = OrganizationMember.objects.create(organization=organization, user=nonmember_user)

    response = member_client.get(_verify_url(organization, target.qr_payload))

    assert response.status_code == 403


def test_verify_member_unauthenticated(client: Client, organization: Organization, member: OrganizationMember) -> None:
    response = client.get(_verify_url(organization, member.qr_payload))

    assert response.status_code == 401


# --- 6. Member of another org -> 404; unknown uuid -> 404 ---


@pytest.fixture
def other_org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="other_org_owner_verify")
    return Organization.objects.create(name="Other Org Verify", slug="other-org-verify", owner=owner)


@pytest.fixture
def other_org_member(other_org: Organization, revel_user_factory: RevelUserFactory) -> OrganizationMember:
    user = revel_user_factory(username="other_org_member_verify")
    return OrganizationMember.objects.create(organization=other_org, user=user)


def test_verify_member_cross_org_404s(
    organization_owner_client: Client, organization: Organization, other_org_member: OrganizationMember
) -> None:
    response = organization_owner_client.get(_verify_url(organization, other_org_member.qr_payload))

    assert response.status_code == 404


def test_verify_member_unknown_uuid_404s(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_verify_url(organization, str(uuid.uuid4())))

    assert response.status_code == 404


# --- 7. PAUSED, CANCELLED, BANNED members -> 200 with the true status in body ---


@pytest.mark.parametrize(
    "status",
    [
        OrganizationMember.MembershipStatus.PAUSED,
        OrganizationMember.MembershipStatus.CANCELLED,
        OrganizationMember.MembershipStatus.BANNED,
    ],
)
def test_verify_member_reports_non_active_status(
    organization_owner_client: Client,
    organization: Organization,
    member: OrganizationMember,
    status: str,
) -> None:
    member.status = status
    member.save(update_fields=["status"])

    response = organization_owner_client.get(_verify_url(organization, member.qr_payload))

    assert response.status_code == 200, response.content
    assert response.json()["status"] == status


# --- 8. Malformed code -> 422 (path pattern) ---


def test_verify_member_malformed_code_422(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_verify_url(organization, "member:not-a-uuid"))

    assert response.status_code == 422
