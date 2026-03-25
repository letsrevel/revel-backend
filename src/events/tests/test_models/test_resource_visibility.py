"""Tests for AdditionalResource.for_user() organization scoping."""

import pytest
from django.contrib.auth.models import AnonymousUser

from accounts.models import RevelUser
from events.models import Organization
from events.models.misc import AdditionalResource
from events.models.mixins import ResourceVisibility

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="res_owner", email="res_owner@example.com", password="pass", email_verified=True
    )


@pytest.fixture
def outsider(django_user_model: type[RevelUser]) -> RevelUser:
    """Authenticated user with no relationship to any org."""
    return django_user_model.objects.create_user(
        username="res_outsider", email="res_outsider@example.com", password="pass"
    )


@pytest.fixture
def staff_only_org(owner: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Staff Only Org",
        slug="staff-only-org",
        owner=owner,
        visibility=Organization.Visibility.STAFF_ONLY,
    )


@pytest.fixture
def public_org(owner: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Public Org",
        slug="public-org-res",
        owner=owner,
        visibility=Organization.Visibility.PUBLIC,
    )


def test_for_user_excludes_resources_from_invisible_orgs(
    outsider: RevelUser, staff_only_org: Organization, public_org: Organization
) -> None:
    """Resources from orgs the user can't see should be filtered out."""
    # Resource in a STAFF_ONLY org — outsider can't see this org
    hidden_resource = AdditionalResource.objects.create(
        organization=staff_only_org,
        name="Hidden Resource",
        resource_type="text",
        text="Secret content",
        visibility=ResourceVisibility.PUBLIC,
    )
    # Resource in a PUBLIC org — outsider can see this org
    visible_resource = AdditionalResource.objects.create(
        organization=public_org,
        name="Visible Resource",
        resource_type="text",
        text="Public content",
        visibility=ResourceVisibility.PUBLIC,
    )

    qs = AdditionalResource.objects.for_user(outsider)

    assert visible_resource in qs
    assert hidden_resource not in qs


def test_for_user_owner_sees_own_org_resources(owner: RevelUser, staff_only_org: Organization) -> None:
    """Owner of a STAFF_ONLY org should still see its resources."""
    resource = AdditionalResource.objects.create(
        organization=staff_only_org,
        name="Owner Resource",
        resource_type="text",
        text="Content",
        visibility=ResourceVisibility.STAFF_ONLY,
    )

    qs = AdditionalResource.objects.for_user(owner)

    assert resource in qs


def test_for_user_anonymous_excludes_resources_from_invisible_orgs(
    staff_only_org: Organization, public_org: Organization
) -> None:
    """Anonymous users should not see PUBLIC resources from invisible orgs."""
    hidden_resource = AdditionalResource.objects.create(
        organization=staff_only_org,
        name="Hidden Public Resource",
        resource_type="text",
        text="Content",
        visibility=ResourceVisibility.PUBLIC,
    )
    visible_resource = AdditionalResource.objects.create(
        organization=public_org,
        name="Visible Public Resource",
        resource_type="text",
        text="Content",
        visibility=ResourceVisibility.PUBLIC,
    )

    qs = AdditionalResource.objects.for_user(AnonymousUser())

    assert visible_resource in qs
    assert hidden_resource not in qs
