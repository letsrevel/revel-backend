"""Shared fixtures for the integrations app."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationStaff, TicketTier
from integrations import registry
from integrations.tests.fake_provider import FakeProvider


@pytest.fixture
def organization_owner_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="int_owner", email="int_owner@example.com", password="pass", email_verified=True
    )


@pytest.fixture
def organization_staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="int_staff", email="int_staff@example.com", password="pass")


@pytest.fixture
def organization(organization_owner_user: RevelUser) -> Organization:
    return Organization.objects.create(name="Int Org", slug="int-org", owner=organization_owner_user)


@pytest.fixture
def staff_member(organization: Organization, organization_staff_user: RevelUser) -> OrganizationStaff:
    return OrganizationStaff.objects.create(organization=organization, user=organization_staff_user)


def _client_for(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    return _client_for(organization_owner_user)


@pytest.fixture
def organization_staff_client(organization_staff_user: RevelUser, staff_member: OrganizationStaff) -> Client:
    return _client_for(organization_staff_user)


@pytest.fixture
def event(organization: Organization) -> Event:
    start = timezone.now() + timedelta(days=30)
    return Event.objects.create(
        organization=organization,
        name="Int Event",
        slug="int-event",
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=start,
        end=start + timedelta(hours=3),
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(event=event, name="General", price=10, total_quantity=100)


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """Register a FakeProvider under key ``fake`` for the duration of the test."""
    provider = FakeProvider()
    monkeypatch.setattr(registry, "PROVIDERS", {"fake": provider})
    return provider
