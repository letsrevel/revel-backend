"""Only the organization owner can create a blacklist entry that matches a current staff member."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationStaff
from events.service import blacklist_service

pytestmark = pytest.mark.django_db

OWNER_ONLY_DETAIL = "Only the organization owner can blacklist a staff member."
UNMATCHED_PHONE = "+4915199990000"

PayloadFor = t.Callable[[RevelUser], dict[str, str]]


@pytest.fixture
def manager_client(organization_staff_client: Client, staff_member: OrganizationStaff) -> Client:
    """A staff member with ``manage_members``: enough to manage the blacklist, not to remove staff."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()
    return organization_staff_client


@pytest.fixture
def other_staff(organization: Organization, django_user_model: type[RevelUser]) -> RevelUser:
    """A second staff member with a verified email, a phone number and a telegram account."""
    from telegram.models import TelegramUser

    user = django_user_model.objects.create_user(
        username="other_staff",
        email="other_staff@example.com",
        password="pass",
        phone_number="+4915122223333",
        email_verified=True,
    )
    TelegramUser.objects.create(telegram_id=4242, telegram_username="Other_Staff_TG", user=user)
    OrganizationStaff.objects.create(organization=organization, user=user)
    return user


def _post(client: Client, organization: Organization, payload: dict[str, str]) -> t.Any:
    url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
    return client.post(url, data=orjson.dumps(payload), content_type="application/json")


MATCHING_PAYLOADS = [
    pytest.param(lambda u: {"user_id": str(u.id)}, id="quick-mode"),
    pytest.param(lambda u: {"email": u.email.upper()}, id="verified-email"),
    pytest.param(lambda u: {"phone_number": u.phone_number}, id="phone"),
    pytest.param(lambda u: {"telegram_username": "@other_staff_tg"}, id="telegram"),
]


@pytest.mark.parametrize("payload_for", MATCHING_PAYLOADS)
def test_non_owner_cannot_blacklist_a_staff_member(
    manager_client: Client, organization: Organization, other_staff: RevelUser, payload_for: PayloadFor
) -> None:
    response = _post(manager_client, organization, payload_for(other_staff))

    assert response.status_code == 403
    assert response.json() == {"detail": OWNER_ONLY_DETAIL}
    assert not Blacklist.objects.filter(organization=organization).exists()
    assert OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()


def test_unverified_email_match_also_needs_the_owner(
    manager_client: Client, organization: Organization, other_staff: RevelUser
) -> None:
    """An unverified email links no account, but the entry still matches the staff member."""
    RevelUser.objects.filter(pk=other_staff.pk).update(email_verified=False)

    response = _post(manager_client, organization, {"email": other_staff.email})

    assert response.status_code == 403
    assert OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()


def test_non_owner_cannot_blacklist_themselves(
    manager_client: Client, organization: Organization, organization_staff_user: RevelUser
) -> None:
    response = _post(manager_client, organization, {"user_id": str(organization_staff_user.id)})

    assert response.status_code == 403
    assert OrganizationStaff.objects.filter(organization=organization, user=organization_staff_user).exists()


@pytest.mark.parametrize("payload_for", MATCHING_PAYLOADS)
def test_owner_can_blacklist_a_staff_member_and_removes_the_staff_role(
    organization_owner_client: Client, organization: Organization, other_staff: RevelUser, payload_for: PayloadFor
) -> None:
    response = _post(organization_owner_client, organization, payload_for(other_staff))

    assert response.status_code == 201
    assert Blacklist.objects.filter(organization=organization).count() == 1
    assert not OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()


def test_non_owner_can_still_blacklist_a_non_staff_account(
    manager_client: Client, organization: Organization, nonmember_user: RevelUser
) -> None:
    response = _post(manager_client, organization, {"user_id": str(nonmember_user.id)})

    assert response.status_code == 201
    assert Blacklist.objects.filter(organization=organization, user=nonmember_user).exists()


@pytest.mark.parametrize(
    "payload_for",
    [
        pytest.param(lambda u: {"user_id": str(u.id)}, id="quick-mode"),
        pytest.param(lambda u: {"email": u.email}, id="verified-email"),
    ],
)
def test_staff_of_another_organization_is_not_a_staff_match(
    manager_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    nonmember_user: RevelUser,
    payload_for: PayloadFor,
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    OrganizationStaff.objects.create(organization=other_org, user=nonmember_user)

    response = _post(manager_client, organization, payload_for(nonmember_user))

    assert response.status_code == 201
    assert OrganizationStaff.objects.filter(organization=other_org, user=nonmember_user).exists()


def test_non_owner_can_still_blacklist_an_unmatched_identifier(
    manager_client: Client, organization: Organization, other_staff: RevelUser
) -> None:
    response = _post(manager_client, organization, {"phone_number": UNMATCHED_PHONE, "first_name": "X"})

    assert response.status_code == 201
    assert OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()


def test_non_owner_can_still_blacklist_identifiers_of_the_owner(
    manager_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """The owner is exempt from the consequences, so a match on the owner is not a staff match."""
    OrganizationStaff.objects.create(organization=organization, user=organization_owner_user)

    response = _post(manager_client, organization, {"email": organization_owner_user.email})

    assert response.status_code == 201
    assert OrganizationStaff.objects.filter(organization=organization, user=organization_owner_user).exists()


def test_update_by_non_owner_cannot_change_the_matched_identity(
    manager_client: Client, organization: Organization, organization_owner_user: RevelUser, other_staff: RevelUser
) -> None:
    """An update changes only the reason and the names, so it never reaches a staff member."""
    entry = Blacklist.objects.create(
        organization=organization, phone_number=UNMATCHED_PHONE, created_by=organization_owner_user
    )
    url = reverse("api:update_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
    payload = {"reason": "Updated", "phone_number": other_staff.phone_number, "email": other_staff.email}

    response = manager_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    entry.refresh_from_db()
    assert entry.reason == "Updated"
    assert (entry.phone_number, entry.email, entry.user) == (UNMATCHED_PHONE, None, None)
    assert OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()


def test_service_rule_applies_to_every_caller(
    organization: Organization, organization_staff_user: RevelUser, other_staff: RevelUser
) -> None:
    """The rule lives in the service, so a caller other than the controller gets the same answer."""
    from ninja.errors import HttpError

    with pytest.raises(HttpError) as exc:
        blacklist_service.add_user_to_blacklist(organization, other_staff.id, created_by=organization_staff_user)

    assert exc.value.status_code == 403
    assert OrganizationStaff.objects.filter(organization=organization, user=other_staff).exists()
