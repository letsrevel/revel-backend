"""Data migration 0029: legacy ``notify_announcements=False`` follows become per-org mutes (#1031)."""

import importlib

import pytest
from django.apps import apps as django_apps

from accounts.models import RevelUser
from events.models import Organization
from events.models.follow import OrganizationFollow
from notifications.models import NotificationPreference

pytestmark = pytest.mark.django_db

# The migration module name starts with a digit, so it can't be a normal import.
_migration = importlib.import_module("notifications.migrations.0029_copy_follow_announcement_mutes")
copy_follow_announcement_mutes = _migration.copy_follow_announcement_mutes


def _user(name: str) -> RevelUser:
    return RevelUser.objects.create_user(username=name, email=f"{name}@example.com")


@pytest.fixture
def orgs() -> tuple[Organization, Organization]:
    owner = _user("migr-owner")
    return (
        Organization.objects.create(name="Org A", slug="migr-org-a", owner=owner),
        Organization.objects.create(name="Org B", slug="migr-org-b", owner=owner),
    )


def _muted(user: RevelUser) -> set[str]:
    prefs = NotificationPreference.objects.get(user=user)
    return {org.slug for org in prefs.muted_organizations.all()}


def test_copies_active_opt_outs_only(orgs: tuple[Organization, Organization]) -> None:
    org_a, org_b = orgs
    opted_out, opted_in, archived = _user("migr-out"), _user("migr-in"), _user("migr-archived")
    OrganizationFollow.objects.create(user=opted_out, organization=org_a, notify_announcements=False)
    OrganizationFollow.objects.create(user=opted_out, organization=org_b, notify_announcements=True)
    OrganizationFollow.objects.create(user=opted_in, organization=org_a, notify_announcements=True)
    OrganizationFollow.objects.create(user=archived, organization=org_a, notify_announcements=False, is_archived=True)

    copy_follow_announcement_mutes(django_apps, None)

    assert _muted(opted_out) == {"migr-org-a"}
    assert _muted(opted_in) == set()
    assert _muted(archived) == set()


def test_creates_missing_preferences(orgs: tuple[Organization, Organization]) -> None:
    user = _user("migr-noprefs")
    NotificationPreference.objects.filter(user=user).delete()
    OrganizationFollow.objects.create(user=user, organization=orgs[0], notify_announcements=False)

    copy_follow_announcement_mutes(django_apps, None)

    assert _muted(user) == {"migr-org-a"}


def test_is_idempotent_and_keeps_existing_mutes(orgs: tuple[Organization, Organization]) -> None:
    org_a, org_b = orgs
    user = _user("migr-rerun")
    user.notification_preferences.muted_organizations.add(org_b)
    OrganizationFollow.objects.create(user=user, organization=org_a, notify_announcements=False)

    copy_follow_announcement_mutes(django_apps, None)
    copy_follow_announcement_mutes(django_apps, None)

    assert _muted(user) == {"migr-org-a", "migr-org-b"}


def test_noop_without_opt_outs() -> None:
    copy_follow_announcement_mutes(django_apps, None)

    assert not NotificationPreference.muted_organizations.through.objects.exists()
