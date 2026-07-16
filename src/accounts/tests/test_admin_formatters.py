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


def _admin_row(user: RevelUser) -> RevelUser:
    """Fetch the user through the admin queryset so count annotations are present."""
    from django.contrib import admin as dj_admin
    from django.test import RequestFactory

    from accounts.admin.user import RevelUserAdmin

    model_admin = RevelUserAdmin(RevelUser, dj_admin.site)
    return model_admin.get_queryset(RequestFactory().get("/admin/")).get(pk=user.pk)


def test_counts_zero_for_fresh_user(user: RevelUser) -> None:
    from django.contrib import admin as dj_admin

    from accounts.admin.user import RevelUserAdmin

    model_admin = RevelUserAdmin(RevelUser, dj_admin.site)
    row = _admin_row(user)
    assert model_admin.event_count(row) == 0
    assert model_admin.organization_count(row) == 0


def test_organization_count_and_participation_for_owned_org(user: RevelUser) -> None:
    from django.contrib import admin as dj_admin

    from accounts.admin.user import RevelUserAdmin
    from events.models import Organization

    org = Organization.objects.create(name="Owned Org", slug="owned-org", owner=user)

    model_admin = RevelUserAdmin(RevelUser, dj_admin.site)
    assert model_admin.organization_count(_admin_row(user)) == 1
    html = formatters.organization_participation(user)
    assert "Organization Roles:" in html
    assert org.name in html
    assert "Owner" in html
    assert str(org.id) in html


def test_organization_participation_escapes_org_name(user: RevelUser) -> None:
    from events.models import Organization

    Organization.objects.create(name="<script>alert(1)</script>", slug="xss-org", owner=user)

    html = formatters.organization_participation(user)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


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
