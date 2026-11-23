"""A blacklisted person cannot be made staff."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationMember, OrganizationStaff
from events.service import organization_service

pytestmark = pytest.mark.django_db

BLACKLISTED_DETAIL = "A blacklisted person cannot be made staff."
PHONE = "+4915144445555"


@pytest.fixture
def candidate(organization: Organization, django_user_model: type[RevelUser]) -> RevelUser:
    """An account with a member row (so it passes the relationship check) and a phone number."""
    user = django_user_model.objects.create_user(
        username="candidate", email="candidate@example.com", password="pass", phone_number=PHONE
    )
    OrganizationMember.objects.create(organization=organization, user=user)
    return user


def _add_staff(client: Client, organization: Organization, user: RevelUser) -> t.Any:
    url = reverse("api:create_organization_staff", kwargs={"slug": organization.slug, "user_id": user.id})
    return client.post(url, content_type="application/json")


@pytest.mark.parametrize(
    "entry_fields",
    [
        pytest.param(lambda u: {"user": u}, id="linked-entry"),
        pytest.param(lambda u: {"email": u.email}, id="email-entry"),
        pytest.param(lambda u: {"email": u.email.upper()}, id="email-entry-other-case"),
        pytest.param(lambda u: {"phone_number": u.phone_number}, id="phone-entry"),
    ],
)
def test_blacklisted_person_cannot_be_made_staff(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    candidate: RevelUser,
    entry_fields: t.Callable[[RevelUser], dict[str, t.Any]],
) -> None:
    Blacklist.objects.create(organization=organization, created_by=organization_owner_user, **entry_fields(candidate))

    response = _add_staff(organization_owner_client, organization, candidate)

    assert response.status_code == 400
    assert response.json() == {"detail": BLACKLISTED_DETAIL}
    assert not OrganizationStaff.objects.filter(organization=organization, user=candidate).exists()


def test_person_blacklisted_elsewhere_can_be_made_staff(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    candidate: RevelUser,
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    Blacklist.objects.create(organization=other_org, user=candidate, created_by=organization_owner_user)

    response = _add_staff(organization_owner_client, organization, candidate)

    assert response.status_code == 201
    assert OrganizationStaff.objects.filter(organization=organization, user=candidate).exists()


def test_unrelated_blacklist_entry_does_not_block_staff(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    candidate: RevelUser,
) -> None:
    Blacklist.objects.create(
        organization=organization, phone_number="+4915100000009", created_by=organization_owner_user
    )

    response = _add_staff(organization_owner_client, organization, candidate)

    assert response.status_code == 201
    assert OrganizationStaff.objects.filter(organization=organization, user=candidate).exists()


def test_service_refuses_a_blacklisted_person(
    organization: Organization, organization_owner_user: RevelUser, candidate: RevelUser
) -> None:
    from ninja.errors import HttpError

    Blacklist.objects.create(organization=organization, phone_number=PHONE, created_by=organization_owner_user)

    with pytest.raises(HttpError) as exc:
        organization_service.add_staff(organization, candidate)

    assert exc.value.status_code == 400
    assert not OrganizationStaff.objects.filter(organization=organization, user=candidate).exists()


def test_owner_is_exempt(organization: Organization, organization_owner_user: RevelUser) -> None:
    """The owner is never banned from their own organization, so a matching entry does not block them."""
    Blacklist.objects.create(
        organization=organization, email=organization_owner_user.email, created_by=organization_owner_user
    )

    staff = organization_service.add_staff(organization, organization_owner_user)

    assert staff.user == organization_owner_user
