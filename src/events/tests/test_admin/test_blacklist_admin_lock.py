"""A blacklist entry saved in the Django admin takes the organization lock first."""

import typing as t

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationMember

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
