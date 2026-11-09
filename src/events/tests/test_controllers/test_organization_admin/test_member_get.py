"""Tests for ``GET /organization-admin/{slug}/members/{user_id}``."""

import uuid

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Organization, OrganizationMember, OrganizationUserNote

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus


def _url(organization: Organization, user_id: uuid.UUID) -> str:
    return reverse("api:get_organization_member", kwargs={"slug": organization.slug, "user_id": user_id})


@pytest.mark.parametrize("status", list(Status))
def test_returns_row_whatever_its_status(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser, status: Status
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=status)
    response = organization_owner_client.get(_url(organization, member_user.id))
    assert response.status_code == 200, response.content
    assert response.json()["status"] == status.value
    assert response.json()["user"]["email"] == member_user.email


def test_404_for_user_without_row(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    assert organization_owner_client.get(_url(organization, member_user.id)).status_code == 404


def test_404_for_other_orgs_member(
    organization_owner_client: Client,
    organization: Organization,
    member_user: RevelUser,
    revel_user_factory: RevelUserFactory,
) -> None:
    other = Organization.objects.create(name="Other", slug="other-org", owner=revel_user_factory())
    OrganizationMember.objects.create(organization=other, user=member_user)
    assert organization_owner_client.get(_url(organization, member_user.id)).status_code == 404


def test_matches_list_row(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user)
    OrganizationUserNote.objects.create(organization=organization, user=member_user, body="VIP")
    listed = organization_owner_client.get(
        reverse("api:list_organization_members", kwargs={"slug": organization.slug})
    ).json()["results"]
    single = organization_owner_client.get(_url(organization, member_user.id)).json()
    assert listed == [single]
    assert single["private_note"] == "VIP"


def test_staff_without_manage_members_gets_null_note(
    organization_staff_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user)
    OrganizationUserNote.objects.create(organization=organization, user=member_user, body="hidden")
    response = organization_staff_client.get(_url(organization, member_user.id))
    assert response.status_code == 200
    assert response.json()["private_note"] is None
    assert "hidden" not in response.content.decode()


def test_non_staff_forbidden(member_client: Client, organization: Organization, member_user: RevelUser) -> None:
    # ``member_client`` already created the member's row.
    assert member_client.get(_url(organization, member_user.id)).status_code == 403
