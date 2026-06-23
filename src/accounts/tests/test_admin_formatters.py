"""Tests for the RevelUserAdmin display formatters (accounts/admin/formatters.py)."""

import typing as t

import pytest

from accounts.admin import formatters
from accounts.models import RevelUser

pytestmark = pytest.mark.django_db


def test_profile_thumbnail_fallback_uses_username_initial(user: RevelUser) -> None:
    html = formatters.profile_thumbnail(user)
    # No profile picture set -> initial-letter fallback span.
    assert "<span" in html
    assert user.username[0].upper() in html


def test_profile_thumbnail_fallback_question_mark_when_no_username() -> None:
    # Defensive branch: the manager rejects empty usernames, so use an unsaved instance.
    assert ">?</span>" in formatters.profile_thumbnail(RevelUser(username=""))


def test_profile_image_preview_without_picture(user: RevelUser) -> None:
    assert formatters.profile_image_preview(user) == "No profile picture"


def test_counts_zero_for_fresh_user(user: RevelUser) -> None:
    assert formatters.event_count(user) == 0
    assert formatters.organization_count(user) == 0


def test_organization_count_and_participation_for_owned_org(user: RevelUser) -> None:
    from events.models import Organization

    org = Organization.objects.create(name="Owned Org", slug="owned-org", owner=user)

    assert formatters.organization_count(user) == 1
    html = formatters.organization_participation(user)
    assert "Organization Roles:" in html
    assert org.name in html
    assert "Owner" in html
    assert str(org.id) in html


def test_event_participation_empty(user: RevelUser) -> None:
    html = formatters.event_participation(user)
    assert "Event Participation:" in html
    assert "<ul></ul>" in html


def test_impersonate_link_dash_for_staff(revel_user_factory: t.Callable[..., RevelUser]) -> None:
    staff = revel_user_factory(username="staff@example.com", email="staff@example.com", is_staff=True)
    assert "—" in formatters.impersonate_link(staff)


def test_impersonate_link_for_regular_user(user: RevelUser) -> None:
    html = formatters.impersonate_link(user)
    assert "Impersonate" in html
    assert str(user.pk) in html
