"""Shared fixtures for polls tests."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from events.models.event import Event
from events.models.organization import Organization
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
