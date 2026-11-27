"""A blacklist entry stays in place when its linked account is deleted or erased."""

import typing as t

import pytest

from accounts.models import RevelUser
from accounts.service.account_erasure import erase_account_keeping_holdings
from events.models import Blacklist, Organization
from events.service.account_deletion_service import KeptHoldings
from events.service.blacklist_service import check_user_hard_blacklisted

pytestmark = pytest.mark.django_db

EMAIL = "retained@example.com"
PHONE = "+436601234567"
TELEGRAM = "retained_handle"


@pytest.fixture
def org(django_user_model: type[RevelUser]) -> Organization:
    """An organization with its own owner."""
    owner = django_user_model.objects.create_user(username="ret_owner", email="ret_owner@example.com", password="pass")
    return Organization.objects.create(name="Retention Org", slug="retention-org", owner=owner)


@pytest.fixture
def other_org(django_user_model: type[RevelUser]) -> Organization:
    """A second organization."""
    owner = django_user_model.objects.create_user(
        username="ret_owner2", email="ret_owner2@example.com", password="pass"
    )
    return Organization.objects.create(name="Retention Org 2", slug="retention-org-2", owner=owner)


@pytest.fixture
def account(django_user_model: type[RevelUser]) -> RevelUser:
    """The account that is deleted in the tests."""
    return django_user_model.objects.create_user(username="ret_user", email=EMAIL, password="pass")


def _linked_entry(org: Organization, user: RevelUser, **extra: t.Any) -> Blacklist:
    values: dict[str, t.Any] = {
        "email": EMAIL,
        "phone_number": PHONE,
        "telegram_username": TELEGRAM,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "preferred_name": "Ad",
        "reason": "linked reason",
    }
    values.update(extra)
    return Blacklist.objects.create(organization=org, user=user, **values)


def _assert_kept_unlinked(org: Organization) -> Blacklist:
    entry = Blacklist.objects.get(organization=org)
    assert entry.user_id is None
    assert (entry.email, entry.phone_number, entry.telegram_username) == (EMAIL, PHONE, TELEGRAM)
    assert entry.reason == "linked reason"
    assert (entry.first_name, entry.last_name, entry.preferred_name) == (None, None, None)
    return entry


def test_full_delete_keeps_the_entry_unlinked(org: Organization, account: RevelUser) -> None:
    _linked_entry(org, account)

    account.delete()

    _assert_kept_unlinked(org)


def test_erase_in_place_keeps_the_entry_unlinked(org: Organization, account: RevelUser) -> None:
    _linked_entry(org, account)

    erase_account_keeping_holdings(account, KeptHoldings(ticket_ids=frozenset(), pass_ids=frozenset()))

    _assert_kept_unlinked(org)


def test_a_new_account_with_the_same_email_is_blacklisted_and_linked(
    org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    _linked_entry(org, account, phone_number=None, telegram_username=None)
    account.delete()

    new_account = django_user_model.objects.create_user(username="ret_new", email=EMAIL, password="pass")

    assert check_user_hard_blacklisted(new_account, org)
    assert Blacklist.objects.get(organization=org).user_id == new_account.pk


def test_merge_into_the_unlinked_entry_that_holds_the_email(org: Organization, account: RevelUser) -> None:
    existing = Blacklist.objects.create(organization=org, email=EMAIL, reason="existing reason", first_name="Old")
    _linked_entry(org, account, telegram_username=None)

    account.delete()

    merged = Blacklist.objects.get(organization=org)
    assert merged.pk == existing.pk
    assert (merged.email, merged.phone_number) == (EMAIL, PHONE)
    assert merged.reason == "existing reason"
    assert merged.first_name == "Old"
    assert merged.user_id is None


def test_merge_fills_an_empty_reason_of_the_target(org: Organization, account: RevelUser) -> None:
    existing = Blacklist.objects.create(organization=org, email=EMAIL, reason="")
    linked = _linked_entry(org, account, telegram_username=None)

    account.delete()

    existing.refresh_from_db()
    assert existing.reason == linked.reason != ""


def test_merge_skips_an_identifier_held_by_a_third_entry(org: Organization, account: RevelUser) -> None:
    existing = Blacklist.objects.create(organization=org, email=EMAIL, reason="existing reason")
    third = Blacklist.objects.create(organization=org, phone_number=PHONE, reason="third reason")
    _linked_entry(org, account)

    account.delete()

    assert Blacklist.objects.filter(organization=org).count() == 2
    existing.refresh_from_db()
    third.refresh_from_db()
    assert (existing.email, existing.phone_number, existing.telegram_username) == (EMAIL, None, TELEGRAM)
    assert (third.email, third.phone_number, third.reason) == (None, PHONE, "third reason")


def test_bulk_queryset_delete_keeps_the_entries(
    org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    second = django_user_model.objects.create_user(username="ret_user2", email="ret2@example.com", password="pass")
    _linked_entry(org, account)
    Blacklist.objects.create(organization=org, user=second, email="ret2@example.com", reason="second reason")

    RevelUser.objects.filter(pk__in=[account.pk, second.pk]).delete()

    entries = Blacklist.objects.filter(organization=org)
    assert entries.count() == 2
    assert not entries.filter(user__isnull=False).exists()
    assert set(entries.values_list("email", flat=True)) == {EMAIL, "ret2@example.com"}


def test_entries_of_other_accounts_and_organizations_are_untouched(
    org: Organization, other_org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    bystander = django_user_model.objects.create_user(username="ret_bystander", email="by@example.com", password="pass")
    own = _linked_entry(org, account)
    others_entry = Blacklist.objects.create(
        organization=org, user=bystander, email="by@example.com", first_name="Bee", reason="bystander"
    )
    unlinked_elsewhere = Blacklist.objects.create(
        organization=other_org, email=EMAIL, first_name="Eve", reason="elsewhere"
    )

    account.delete()

    others_entry.refresh_from_db()
    unlinked_elsewhere.refresh_from_db()
    assert others_entry.user_id == bystander.pk
    assert others_entry.first_name == "Bee"
    assert (unlinked_elsewhere.first_name, unlinked_elsewhere.reason) == ("Eve", "elsewhere")
    assert Blacklist.objects.filter(pk=own.pk).exists()
    assert Blacklist.objects.get(pk=own.pk).user_id is None
