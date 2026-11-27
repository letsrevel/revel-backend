"""A blacklist entry saved in the Django admin takes the organization lock first."""

import typing as t
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationMember
from events.service import blacklist_service

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings: t.Any) -> None:
    """Admin pages render static URLs; the hashed manifest storage has no manifest in tests."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(superuser: RevelUser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


def test_admin_save_locks_the_organization_before_the_insert(admin_client: Client, organization: Organization) -> None:
    data = {
        "organization": str(organization.pk),
        "email": "admin-added@example.com",
        "reason": "admin",
    }

    with CaptureQueriesContext(connection) as ctx:
        response = admin_client.post(reverse("admin:events_blacklist_add"), data)

    assert response.status_code == 302, response.content[:500]
    assert Blacklist.objects.filter(organization=organization, email="admin-added@example.com").exists()
    sql = [q["sql"] for q in ctx.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "FOR NO KEY UPDATE" in q and '"events_organization"' in q)
    insert = next(i for i, q in enumerate(sql) if q.startswith('INSERT INTO "events_blacklist"'))
    assert lock < insert


def test_a_stale_change_form_does_not_clear_an_identifier_kept_after_a_deletion(
    admin_client: Client, organization: Organization, django_user_model: type[RevelUser]
) -> None:
    """The form is loaded, a deletion moves a phone number onto the entry, then the old form is saved."""
    account = django_user_model.objects.create_user(
        username="kept_account", email="kept@example.com", password="x", phone_number="+393331234000"
    )
    OrganizationMember.objects.create(organization=organization, user=account)
    Blacklist.objects.create(organization=organization, user=account, email="kept@example.com", reason="linked")
    holder = Blacklist.objects.create(organization=organization, email="kept@example.com", reason="first")
    url = reverse("admin:events_blacklist_change", args=[holder.pk])
    assert admin_client.get(url).status_code == 200  # the admin opens the form; phone_number is empty

    account.delete()  # the deletion keeps the account's phone number on the holder entry
    holder.refresh_from_db()
    assert holder.phone_number == "+393331234000"

    stale = {  # every field of the form as it was rendered, before the deletion
        "organization": str(organization.pk),
        "reason": "changed",
        "email": "kept@example.com",
        "phone_number": "",
        "telegram_username": "",
    }
    response = admin_client.post(url, stale)

    assert response.status_code == 302, response.content[:500]
    holder.refresh_from_db()
    assert holder.reason == "changed"  # free-text fields stay editable
    assert holder.phone_number == "+393331234000"
    assert holder.email == "kept@example.com"


def test_the_add_view_still_accepts_every_identifier(admin_client: Client, organization: Organization) -> None:
    data = {
        "organization": str(organization.pk),
        "email": "new-entry@example.com",
        "phone_number": "+393331234111",
        "telegram_username": "new_handle",
        "reason": "admin",
    }

    response = admin_client.post(reverse("admin:events_blacklist_add"), data)

    assert response.status_code == 302, response.content[:500]
    entry = Blacklist.objects.get(organization=organization, email="new-entry@example.com")
    assert (entry.phone_number, entry.telegram_username) == ("+393331234111", "new_handle")


def _linked_setup(org: Organization, django_user_model: type[RevelUser]) -> tuple[RevelUser, Blacklist, Blacklist]:
    account = django_user_model.objects.create_user(
        username="race_account", email="race@example.com", password="x", phone_number="+393331234222"
    )
    OrganizationMember.objects.create(organization=org, user=account)
    linked = Blacklist.objects.create(
        organization=org, user=account, email="race@example.com", reason="linked", first_name="Mario", last_name="R"
    )
    holder = Blacklist.objects.create(organization=org, email="other@example.com", reason="holder")
    return account, linked, holder


def test_a_stale_change_form_does_not_write_back_the_names_a_deletion_cleared(
    admin_client: Client, organization: Organization, django_user_model: type[RevelUser]
) -> None:
    account, linked, _holder = _linked_setup(organization, django_user_model)
    url = reverse("admin:events_blacklist_change", args=[linked.pk])
    assert admin_client.get(url).status_code == 200  # the form shows the names

    account.delete()  # the entry stays unlinked, without names
    stale = {
        "organization": str(organization.pk),
        "user": str(account.pk),
        "reason": "changed",
        "email": "race@example.com",
        "first_name": "Mario",
        "last_name": "R",
    }
    response = admin_client.post(url, stale)

    assert response.status_code == 302, response.content[:500]
    linked.refresh_from_db()
    assert (linked.reason, linked.user_id, linked.first_name, linked.last_name) == ("changed", None, None, None)


def test_a_deletion_between_the_admin_load_and_the_lock_is_not_overwritten(
    admin_client: Client, organization: Organization, django_user_model: type[RevelUser]
) -> None:
    """The admin loads the row, then a deletion commits before the admin save takes the lock."""
    account, linked, _holder = _linked_setup(organization, django_user_model)
    original_lock = blacklist_service.lock_organization_for_staff_changes

    def deletion_first(org: Organization) -> None:
        if RevelUser.objects.filter(pk=account.pk).exists():
            account.delete()
        original_lock(org)

    url = reverse("admin:events_blacklist_change", args=[linked.pk])
    with patch.object(blacklist_service, "lock_organization_for_staff_changes", side_effect=deletion_first):
        response = admin_client.post(url, {"organization": str(organization.pk), "reason": "changed"})

    assert response.status_code == 302, response.content[:500]
    linked.refresh_from_db()
    assert (linked.reason, linked.user_id, linked.first_name) == ("changed", None, None)
    assert linked.phone_number == "+393331234222"  # kept from the account by the deletion


def test_an_entry_merged_away_during_the_admin_save_is_not_inserted_again(
    admin_client: Client, organization: Organization, django_user_model: type[RevelUser]
) -> None:
    """The deletion merges the entry into a holder between the admin load and the lock: 404, no re-insert."""
    account = django_user_model.objects.create_user(username="merged_account", email="merged@example.com", password="x")
    linked = Blacklist.objects.create(organization=organization, user=account, email="merged@example.com", reason="l")
    Blacklist.objects.create(organization=organization, email="merged@example.com", reason="holder")
    original_lock = blacklist_service.lock_organization_for_staff_changes

    def deletion_first(org: Organization) -> None:
        if RevelUser.objects.filter(pk=account.pk).exists():
            account.delete()
        original_lock(org)

    url = reverse("admin:events_blacklist_change", args=[linked.pk])
    with patch.object(blacklist_service, "lock_organization_for_staff_changes", side_effect=deletion_first):
        response = admin_client.post(url, {"organization": str(organization.pk), "reason": "changed"})

    # The test runs the deletion inside the admin request, so the 404 rolls it back too; in production the deletion
    # commits on its own. What matters here: the save answers 404 and inserts no row.
    assert response.status_code == 404
    assert Blacklist.objects.filter(organization=organization, email="merged@example.com").count() == 2
