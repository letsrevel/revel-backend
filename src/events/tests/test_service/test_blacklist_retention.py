"""A blacklist entry stays in place when its linked account is deleted or erased."""

import typing as t
from unittest.mock import patch

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.models import RevelUser
from accounts.service.account_erasure import erase_account_keeping_holdings
from events.models import Blacklist, Organization, OrganizationMember
from events.service.account_deletion_service import KeptHoldings
from events.service.blacklist_service import check_user_hard_blacklisted
from telegram.models import TelegramUser

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


def _assert_every_identifier_held(org: Organization, **identifiers: str) -> None:
    """Every given identifier is held by some unlinked entry of the organization (email and Telegram without case)."""
    entries = list(Blacklist.objects.filter(organization=org, user__isnull=True))
    for field, value in identifiers.items():
        assert any(
            (stored := getattr(e, field))
            and (stored.lower() == value.lower() if field != "phone_number" else stored == value)
            for e in entries
        ), f"{field} {value} is not held by an unlinked entry"


def test_identifier_the_holder_cannot_take_stays_on_a_kept_unlinked_entry(
    org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    existing = Blacklist.objects.create(
        organization=org, phone_number=PHONE, email="other@example.com", reason="existing reason"
    )
    linked = _linked_entry(org, account)

    account.delete()

    assert Blacklist.objects.filter(organization=org).count() == 2
    kept = Blacklist.objects.get(pk=linked.pk)
    assert kept.user_id is None
    assert (kept.email, kept.phone_number, kept.telegram_username) == (EMAIL, None, None)
    assert (kept.first_name, kept.last_name, kept.preferred_name) == (None, None, None)
    assert kept.reason == "linked reason"
    existing.refresh_from_db()
    assert (existing.email, existing.phone_number, existing.telegram_username) == ("other@example.com", PHONE, TELEGRAM)
    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username=TELEGRAM)

    new_account = django_user_model.objects.create_user(username="ret_new2", email=EMAIL, password="pass")
    assert check_user_hard_blacklisted(new_account, org)


def test_merge_recognises_a_holder_whose_email_differs_only_in_case(org: Organization, account: RevelUser) -> None:
    existing = Blacklist.objects.create(organization=org, email="placeholder@example.com", reason="existing reason")
    # A queryset update skips the save-time normalization, so the stored email keeps its case.
    Blacklist.objects.filter(pk=existing.pk).update(email="Retained@Example.com")
    _linked_entry(org, account)

    account.delete()

    merged = Blacklist.objects.get(organization=org)
    assert merged.pk == existing.pk
    assert (merged.email, merged.phone_number, merged.telegram_username) == ("Retained@Example.com", PHONE, TELEGRAM)
    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username=TELEGRAM)


def test_merge_recognises_a_holder_whose_telegram_differs_only_in_case(org: Organization, account: RevelUser) -> None:
    existing = Blacklist.objects.create(organization=org, telegram_username="placeholder", reason="existing reason")
    Blacklist.objects.filter(pk=existing.pk).update(telegram_username=TELEGRAM.upper())
    _linked_entry(org, account)

    account.delete()

    assert Blacklist.objects.filter(organization=org).count() == 1
    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username=TELEGRAM)


def test_erase_in_place_merges_into_the_holder_and_keeps_every_identifier(
    org: Organization, account: RevelUser
) -> None:
    existing = Blacklist.objects.create(
        organization=org, phone_number=PHONE, email="other@example.com", reason="existing reason"
    )
    _linked_entry(org, account)

    erase_account_keeping_holdings(account, KeptHoldings(ticket_ids=frozenset(), pass_ids=frozenset()))

    assert not Blacklist.objects.filter(user__isnull=False).exists()
    existing.refresh_from_db()
    assert existing.telegram_username == TELEGRAM
    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username=TELEGRAM)


def test_leftover_identifier_goes_to_the_oldest_holder_with_an_empty_field(
    org: Organization, account: RevelUser
) -> None:
    h1 = Blacklist.objects.create(organization=org, telegram_username=TELEGRAM, email="x@example.com", reason="h1")
    h2 = Blacklist.objects.create(organization=org, phone_number=PHONE, reason="h2")
    linked = _linked_entry(org, account)

    account.delete()

    assert not Blacklist.objects.filter(pk=linked.pk).exists()
    h1.refresh_from_db()
    h2.refresh_from_db()
    assert h1.email == "x@example.com"
    assert h2.email == EMAIL
    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username=TELEGRAM)


def test_entry_without_any_identifier_is_deleted(org: Organization, account: RevelUser) -> None:
    Blacklist.objects.create(organization=org, user=account, first_name="Ada", reason="no identifier")
    RevelUser.objects.filter(pk=account.pk).update(email="")
    account.refresh_from_db()

    account.delete()

    assert not Blacklist.objects.filter(organization=org).exists()


def test_detach_logs_only_after_commit(
    org: Organization, account: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    _linked_entry(org, account)

    with patch("events.service.blacklist_retention.logger") as logger:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            account.delete()
        assert not [c for c in logger.info.call_args_list if c.args[0] == "blacklist_entries_detached"]
        for callback in callbacks:
            callback()

    calls = [c for c in logger.info.call_args_list if c.args[0] == "blacklist_entries_detached"]
    assert len(calls) == 1
    assert (calls[0].kwargs["count"], calls[0].kwargs["kept"], calls[0].kwargs["merged_away"]) == (1, 1, 0)


NEW_EMAIL = "renamed@example.com"
NEW_PHONE = "+436609999999"
_NO_HOLDINGS = KeptHoldings(ticket_ids=frozenset(), pass_ids=frozenset())


def test_the_current_email_of_the_account_is_kept_with_the_stored_one(
    org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    _linked_entry(org, account, phone_number=None, telegram_username=None)
    account.email = NEW_EMAIL
    account.save(update_fields=["email"])

    account.delete()

    old_holder = django_user_model.objects.create_user(username="ret_old", email=EMAIL, password="pass")
    new_holder = django_user_model.objects.create_user(username="ret_new_mail", email=NEW_EMAIL, password="pass")
    assert check_user_hard_blacklisted(old_holder, org)
    assert check_user_hard_blacklisted(new_holder, org)


def test_an_entry_without_stored_identifiers_takes_them_from_the_account(org: Organization, account: RevelUser) -> None:
    Blacklist.objects.create(organization=org, user=account, first_name="Ada", reason="fk only")

    account.delete()

    entry = Blacklist.objects.get(organization=org)
    assert entry.user_id is None
    assert entry.email == EMAIL
    assert entry.reason == "fk only"


def _give_phone_and_telegram(account: RevelUser) -> None:
    account.phone_number = PHONE
    account.save(update_fields=["phone_number"])
    TelegramUser.objects.create(user=account, telegram_id=7001, telegram_username="@Current_Handle")


def test_the_current_phone_and_telegram_are_kept_for_a_known_holder(org: Organization, account: RevelUser) -> None:
    _give_phone_and_telegram(account)
    OrganizationMember.objects.create(organization=org, user=account)
    Blacklist.objects.create(organization=org, user=account, email=EMAIL, reason="r")

    account.delete()

    _assert_every_identifier_held(org, email=EMAIL, phone_number=PHONE, telegram_username="current_handle")


def test_the_current_phone_and_telegram_are_not_kept_for_a_stranger(org: Organization, account: RevelUser) -> None:
    _give_phone_and_telegram(account)
    Blacklist.objects.create(organization=org, user=account, email=EMAIL, reason="r")

    account.delete()

    entries = Blacklist.objects.filter(organization=org)
    assert set(entries.values_list("email", flat=True)) == {EMAIL}
    assert not entries.exclude(phone_number=None).exists()
    assert not entries.exclude(telegram_username=None).exists()


def test_a_lost_race_keeps_the_other_extra_identifiers(org: Organization, account: RevelUser) -> None:
    """A concurrent entry takes one extra value between the check and the insert: the others are still kept."""
    from events.service import blacklist_retention

    OrganizationMember.objects.create(organization=org, user=account)
    _linked_entry(org, account, telegram_username=None)
    account.email = NEW_EMAIL
    account.phone_number = NEW_PHONE
    account.save(update_fields=["email", "phone_number"])
    original = blacklist_retention._free_values
    calls: list[int] = []

    def racing_free_values(entry: Blacklist, extras: t.Any) -> dict[str, str]:
        values = original(entry, extras)
        if not calls:  # a concurrent save takes the phone after the check, outside the insert's savepoint
            Blacklist.objects.bulk_create([Blacklist(organization=org, phone_number=NEW_PHONE, reason="admin")])
        calls.append(1)
        return values

    with patch.object(blacklist_retention, "_free_values", side_effect=racing_free_values):
        account.delete()

    assert len(calls) == 2  # the first insert failed on the real unique constraint, the retry ran
    assert not RevelUser.objects.filter(pk=account.pk).exists()
    _assert_every_identifier_held(org, email=NEW_EMAIL, phone_number=NEW_PHONE)
    assert Blacklist.objects.filter(organization=org, phone_number=NEW_PHONE).count() == 1


def test_two_lost_races_roll_back_the_deletion(org: Organization, account: RevelUser) -> None:
    """When both inserts of the extra entry lose a race, the deletion fails and nothing is lost."""
    from events.service import blacklist_retention

    OrganizationMember.objects.create(organization=org, user=account)
    linked = _linked_entry(org, account, telegram_username=None)
    account.email = NEW_EMAIL
    account.phone_number = NEW_PHONE
    account.save(update_fields=["email", "phone_number"])
    original = blacklist_retention._free_values
    clashes = iter([{"phone_number": NEW_PHONE}, {"email": NEW_EMAIL}])

    def racing_free_values(entry: Blacklist, extras: t.Any) -> dict[str, str]:
        values = original(entry, extras)
        Blacklist.objects.bulk_create([Blacklist(organization=org, reason="admin", **next(clashes))])
        return values

    with patch.object(blacklist_retention, "_free_values", side_effect=racing_free_values):
        # user.delete() runs without its own savepoint; production wraps it in the deletion task's atomic block.
        with pytest.raises(IntegrityError), transaction.atomic():
            account.delete()

    assert RevelUser.objects.filter(pk=account.pk).exists()
    linked.refresh_from_db()
    assert linked.user_id == account.pk


def test_the_identifiers_are_read_fresh_from_the_database(org: Organization, account: RevelUser) -> None:
    Blacklist.objects.create(organization=org, user=account, email=EMAIL, reason="r")
    stale = RevelUser.objects.get(pk=account.pk)
    RevelUser.objects.filter(pk=account.pk).update(email=NEW_EMAIL)

    stale.delete()

    assert set(Blacklist.objects.filter(organization=org).values_list("email", flat=True)) == {EMAIL, NEW_EMAIL}


def test_an_extra_identifier_held_by_another_unlinked_entry_creates_no_entry(
    org: Organization, account: RevelUser
) -> None:
    held = Blacklist.objects.create(organization=org, email=NEW_EMAIL.upper(), reason="held")
    Blacklist.objects.filter(pk=held.pk).update(email=NEW_EMAIL.upper())
    _linked_entry(org, account, phone_number=None, telegram_username=None)
    account.email = NEW_EMAIL
    account.save(update_fields=["email"])

    account.delete()

    assert Blacklist.objects.filter(organization=org).count() == 2
    assert Blacklist.objects.filter(organization=org, email__iexact=NEW_EMAIL).count() == 1


def test_the_extra_entry_copies_the_reason_and_has_no_creator_and_no_names(
    org: Organization, account: RevelUser, django_user_model: type[RevelUser]
) -> None:
    creator = django_user_model.objects.create_user(username="ret_creator", email="creator@example.com", password="x")
    _linked_entry(org, account, phone_number=None, telegram_username=None, created_by=creator)
    account.email = NEW_EMAIL
    account.save(update_fields=["email"])

    account.delete()

    extra = Blacklist.objects.get(organization=org, email=NEW_EMAIL)
    assert extra.user_id is None
    assert (extra.reason, extra.created_by_id) == ("linked reason", None)
    assert (extra.first_name, extra.last_name, extra.preferred_name) == (None, None, None)


def test_the_reason_goes_to_every_holder_that_receives_an_identifier(org: Organization, account: RevelUser) -> None:
    Blacklist.objects.create(organization=org, telegram_username=TELEGRAM, email="x@example.com", reason="oldest")
    receiver = Blacklist.objects.create(organization=org, phone_number=PHONE, reason="")
    _linked_entry(org, account)

    account.delete()

    receiver.refresh_from_db()
    assert receiver.email == EMAIL
    assert receiver.reason == "linked reason"


def test_the_erase_in_place_path_keeps_the_current_email_too(org: Organization, account: RevelUser) -> None:
    Blacklist.objects.create(organization=org, user=account, email="stored-old@example.com", reason="r")

    erase_account_keeping_holdings(account, _NO_HOLDINGS)

    assert set(Blacklist.objects.filter(organization=org).values_list("email", flat=True)) == {
        "stored-old@example.com",
        EMAIL,
    }


def test_the_account_row_is_locked_before_the_organization_rows(org: Organization, account: RevelUser) -> None:
    _linked_entry(org, account)
    user_table = RevelUser._meta.db_table
    org_table = Organization._meta.db_table

    with CaptureQueriesContext(connection) as ctx:
        account.delete()

    sqls = [q["sql"] for q in ctx.captured_queries]
    account_lock = next(i for i, q in enumerate(sqls) if "FOR NO KEY UPDATE" in q and f'"{user_table}"' in q)
    org_lock = next(i for i, q in enumerate(sqls) if "FOR NO KEY UPDATE" in q and f'FROM "{org_table}"' in q)
    assert account_lock < org_lock
