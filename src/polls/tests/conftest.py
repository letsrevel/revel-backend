"""Shared fixtures for polls tests."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models.event import Event
from events.models.organization import Organization, OrganizationStaff
from questionnaires.models import Questionnaire


@pytest.fixture
def organization(revel_user_factory: t.Any) -> Organization:
    """A basic organization for polls tests."""
    owner = revel_user_factory()
    return Organization.objects.create(name="Polls Test Org", slug="polls-test-org", owner=owner)


@pytest.fixture
def questionnaire() -> Questionnaire:
    """A Questionnaire to back a Poll."""
    return Questionnaire.objects.create(name="Polls Test Questionnaire")


@pytest.fixture
def authenticated_client(revel_user_factory: t.Any) -> Client:
    """An authenticated test client for a freshly created user."""
    user: RevelUser = revel_user_factory()
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def anonymous_client() -> Client:
    """An anonymous (unauthenticated) test client."""
    return Client()


@pytest.fixture
def owner_client(organization: Organization) -> Client:
    """Authenticated test client for the organization owner."""
    refresh = RefreshToken.for_user(organization.owner)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def staff_client(organization: Organization, revel_user_factory: t.Any) -> Client:
    """Authenticated test client for an org staff member with ``manage_polls=True``."""
    staff_user: RevelUser = revel_user_factory()
    OrganizationStaff.objects.create(
        organization=organization,
        user=staff_user,
        permissions={
            "default": {
                "view_organization_details": True,
                "create_event": False,
                "create_event_series": False,
                "edit_event_series": False,
                "delete_event_series": False,
                "edit_event": False,
                "delete_event": False,
                "open_event": False,
                "manage_tickets": False,
                "close_event": False,
                "manage_event": False,
                "check_in_attendees": False,
                "invite_to_event": False,
                "edit_organization": False,
                "manage_members": False,
                "manage_potluck": False,
                "create_questionnaire": False,
                "edit_questionnaire": False,
                "delete_questionnaire": False,
                "evaluate_questionnaire": False,
                "send_announcements": False,
                "manage_subscriptions": False,
                "manage_polls": True,
            },
            "event_overrides": {},
        },
    )
    refresh = RefreshToken.for_user(staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def event(organization: Organization) -> Event:
    """A basic event for polls tests that require ``event__isnull=False``."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Polls Test Event",
        slug="polls-test-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=10,
        status="open",
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )
