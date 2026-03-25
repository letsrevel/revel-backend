"""Tests for UNLISTED visibility across organizations, events, and event series.

UNLISTED items are accessible via direct link (like PUBLIC) but hidden from
discovery listings for non-owner/non-staff users.
"""

import typing as t
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    TicketTier,
)
from events.models.misc import AdditionalResource
from events.models.mixins import ResourceVisibility

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="owner", email="owner@example.com", password="pass", email_verified=True
    )


@pytest.fixture
def staff_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="staff", email="staff@example.com", password="pass")


@pytest.fixture
def member(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="member", email="member@example.com", password="pass")


@pytest.fixture
def outsider(django_user_model: type[RevelUser]) -> RevelUser:
    """Authenticated user with no relationship to the org."""
    return django_user_model.objects.create_user(username="outsider", email="outsider@example.com", password="pass")


@pytest.fixture
def superuser(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_superuser(username="super", email="super@example.com", password="pass")


@pytest.fixture
def unlisted_org(owner: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Unlisted Org", slug="unlisted-org", owner=owner, visibility=Organization.Visibility.UNLISTED
    )


@pytest.fixture
def public_org(owner: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Public Org", slug="public-org", owner=owner, visibility=Organization.Visibility.PUBLIC
    )


@pytest.fixture
def org_staff(unlisted_org: Organization, staff_user: RevelUser) -> OrganizationStaff:
    return OrganizationStaff.objects.create(organization=unlisted_org, user=staff_user)


@pytest.fixture
def org_member(unlisted_org: Organization, member: RevelUser) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=unlisted_org, user=member)


@pytest.fixture
def next_week() -> t.Any:
    return timezone.now() + timedelta(days=7)


@pytest.fixture
def unlisted_event(unlisted_org: Organization, next_week: t.Any) -> Event:
    return Event.objects.create(
        organization=unlisted_org,
        name="Unlisted Event",
        slug="unlisted-event",
        visibility=Event.Visibility.UNLISTED,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        start=next_week,
    )


@pytest.fixture
def unlisted_event_on_public_org(public_org: Organization, next_week: t.Any) -> Event:
    return Event.objects.create(
        organization=public_org,
        name="Unlisted Event Public Org",
        slug="unlisted-event-public-org",
        visibility=Event.Visibility.UNLISTED,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        start=next_week,
    )


@pytest.fixture
def public_event_on_unlisted_org(unlisted_org: Organization, next_week: t.Any) -> Event:
    return Event.objects.create(
        organization=unlisted_org,
        name="Public Event Unlisted Org",
        slug="public-event-unlisted-org",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        start=next_week,
    )


@pytest.fixture
def unlisted_series(unlisted_org: Organization) -> EventSeries:
    return EventSeries.objects.create(organization=unlisted_org, name="Unlisted Series", slug="unlisted-series")


@pytest.fixture
def series_on_public_org(public_org: Organization) -> EventSeries:
    return EventSeries.objects.create(organization=public_org, name="Public Series", slug="public-series")


# --- Organization Visibility ---


class TestOrganizationUnlistedVisibility:
    """Test that UNLISTED orgs are accessible but not discoverable."""

    def test_anonymous_can_access_unlisted_org(self, unlisted_org: Organization) -> None:
        qs = Organization.objects.for_user(AnonymousUser())
        assert unlisted_org in qs

    def test_anonymous_cannot_discover_unlisted_org(self, unlisted_org: Organization) -> None:
        qs = Organization.objects.discoverable_for_user(AnonymousUser())
        assert unlisted_org not in qs

    def test_outsider_can_access_unlisted_org(self, unlisted_org: Organization, outsider: RevelUser) -> None:
        qs = Organization.objects.for_user(outsider)
        assert unlisted_org in qs

    def test_outsider_cannot_discover_unlisted_org(self, unlisted_org: Organization, outsider: RevelUser) -> None:
        qs = Organization.objects.discoverable_for_user(outsider)
        assert unlisted_org not in qs

    def test_owner_can_discover_unlisted_org(self, unlisted_org: Organization, owner: RevelUser) -> None:
        qs = Organization.objects.discoverable_for_user(owner)
        assert unlisted_org in qs

    def test_staff_can_discover_unlisted_org(
        self, unlisted_org: Organization, staff_user: RevelUser, org_staff: OrganizationStaff
    ) -> None:
        qs = Organization.objects.discoverable_for_user(staff_user)
        assert unlisted_org in qs

    def test_superuser_can_discover_unlisted_org(self, unlisted_org: Organization, superuser: RevelUser) -> None:
        qs = Organization.objects.discoverable_for_user(superuser)
        assert unlisted_org in qs

    def test_banned_user_cannot_access_unlisted_org(self, unlisted_org: Organization, outsider: RevelUser) -> None:
        OrganizationMember.objects.create(
            organization=unlisted_org, user=outsider, status=OrganizationMember.MembershipStatus.BANNED
        )
        qs = Organization.objects.for_user(outsider)
        assert unlisted_org not in qs


# --- Event Visibility ---


class TestEventUnlistedVisibility:
    """Test that UNLISTED events are accessible but not discoverable."""

    def test_anonymous_can_access_unlisted_event(self, unlisted_event: Event) -> None:
        qs = Event.objects.for_user(AnonymousUser(), include_past=True)
        assert unlisted_event in qs

    def test_anonymous_cannot_discover_unlisted_event(self, unlisted_event: Event) -> None:
        qs = Event.objects.discoverable_for_user(AnonymousUser(), include_past=True)
        assert unlisted_event not in qs

    def test_outsider_can_access_unlisted_event(self, unlisted_event: Event, outsider: RevelUser) -> None:
        qs = Event.objects.for_user(outsider, include_past=True)
        assert unlisted_event in qs

    def test_outsider_cannot_discover_unlisted_event(self, unlisted_event: Event, outsider: RevelUser) -> None:
        qs = Event.objects.discoverable_for_user(outsider, include_past=True)
        assert unlisted_event not in qs

    def test_owner_can_discover_unlisted_event(self, unlisted_event: Event, owner: RevelUser) -> None:
        qs = Event.objects.discoverable_for_user(owner, include_past=True)
        assert unlisted_event in qs

    def test_staff_can_discover_unlisted_event(
        self, unlisted_event: Event, staff_user: RevelUser, org_staff: OrganizationStaff
    ) -> None:
        qs = Event.objects.discoverable_for_user(staff_user, include_past=True)
        assert unlisted_event in qs

    def test_superuser_can_discover_unlisted_event(self, unlisted_event: Event, superuser: RevelUser) -> None:
        qs = Event.objects.discoverable_for_user(superuser, include_past=True)
        assert unlisted_event in qs


# --- Edge Cases: Mixed Visibility ---


class TestMixedVisibilityEdgeCases:
    """Test combinations of UNLISTED org with PUBLIC event and vice versa."""

    def test_public_event_on_unlisted_org_accessible_to_outsider(
        self, public_event_on_unlisted_org: Event, outsider: RevelUser
    ) -> None:
        """A PUBLIC event on an UNLISTED org is accessible (org is accessible via direct link)."""
        qs = Event.objects.for_user(outsider, include_past=True)
        assert public_event_on_unlisted_org in qs

    def test_public_event_on_unlisted_org_discoverable(
        self, public_event_on_unlisted_org: Event, outsider: RevelUser
    ) -> None:
        """A PUBLIC event on an UNLISTED org appears in discovery (event itself is PUBLIC)."""
        qs = Event.objects.discoverable_for_user(outsider, include_past=True)
        assert public_event_on_unlisted_org in qs

    def test_unlisted_event_on_public_org_accessible(
        self, unlisted_event_on_public_org: Event, outsider: RevelUser
    ) -> None:
        """An UNLISTED event on a PUBLIC org is accessible via direct link."""
        qs = Event.objects.for_user(outsider, include_past=True)
        assert unlisted_event_on_public_org in qs

    def test_unlisted_event_on_public_org_not_discoverable(
        self, unlisted_event_on_public_org: Event, outsider: RevelUser
    ) -> None:
        """An UNLISTED event on a PUBLIC org does NOT appear in discovery for outsiders."""
        qs = Event.objects.discoverable_for_user(outsider, include_past=True)
        assert unlisted_event_on_public_org not in qs


# --- Event Series Visibility ---


class TestEventSeriesUnlistedVisibility:
    """Test that event series on UNLISTED orgs follow the same access/discovery split."""

    def test_anonymous_can_access_series_on_unlisted_org(self, unlisted_series: EventSeries) -> None:
        qs = EventSeries.objects.for_user(AnonymousUser())
        assert unlisted_series in qs

    def test_anonymous_cannot_discover_series_on_unlisted_org(self, unlisted_series: EventSeries) -> None:
        qs = EventSeries.objects.discoverable_for_user(AnonymousUser())
        assert unlisted_series not in qs

    def test_outsider_cannot_discover_series_on_unlisted_org(
        self, unlisted_series: EventSeries, outsider: RevelUser
    ) -> None:
        qs = EventSeries.objects.discoverable_for_user(outsider)
        assert unlisted_series not in qs

    def test_owner_can_discover_series_on_unlisted_org(self, unlisted_series: EventSeries, owner: RevelUser) -> None:
        qs = EventSeries.objects.discoverable_for_user(owner)
        assert unlisted_series in qs

    def test_staff_can_discover_series_on_unlisted_org(
        self, unlisted_series: EventSeries, staff_user: RevelUser, org_staff: OrganizationStaff
    ) -> None:
        qs = EventSeries.objects.discoverable_for_user(staff_user)
        assert unlisted_series in qs


# --- Ticket Tier Visibility ---


class TestTicketTierUnlistedVisibility:
    """Test that tiers on UNLISTED events are visible to users who access the event."""

    @pytest.fixture
    def public_tier_on_unlisted_event(self, unlisted_event_on_public_org: Event) -> TicketTier:
        return TicketTier.objects.create(
            event=unlisted_event_on_public_org,
            name="Public Tier",
            visibility=TicketTier.Visibility.PUBLIC,
        )

    @pytest.fixture
    def unlisted_tier_on_public_event(self, public_org: Organization, next_week: t.Any) -> TicketTier:
        event = Event.objects.create(
            organization=public_org,
            name="Public Event for Tier Test",
            slug="public-event-tier-test",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status=Event.EventStatus.OPEN,
            max_attendees=100,
            start=next_week,
            requires_ticket=False,
        )
        return TicketTier.objects.create(
            event=event,
            name="Unlisted Tier",
            visibility=TicketTier.Visibility.UNLISTED,
        )

    def test_anonymous_sees_public_tier_on_unlisted_event(self, public_tier_on_unlisted_event: TicketTier) -> None:
        """Anonymous users can see PUBLIC tiers on UNLISTED events (event is accessible)."""
        qs = TicketTier.objects.for_user(AnonymousUser())
        assert public_tier_on_unlisted_event in qs

    def test_anonymous_sees_unlisted_tier_on_public_event(self, unlisted_tier_on_public_event: TicketTier) -> None:
        """Anonymous users can see UNLISTED tiers on PUBLIC events (tier treated like PUBLIC)."""
        qs = TicketTier.objects.for_user(AnonymousUser())
        assert unlisted_tier_on_public_event in qs

    def test_outsider_sees_public_tier_on_unlisted_event(
        self, public_tier_on_unlisted_event: TicketTier, outsider: RevelUser
    ) -> None:
        qs = TicketTier.objects.for_user(outsider)
        assert public_tier_on_unlisted_event in qs

    def test_outsider_sees_unlisted_tier_on_public_event(
        self, unlisted_tier_on_public_event: TicketTier, outsider: RevelUser
    ) -> None:
        qs = TicketTier.objects.for_user(outsider)
        assert unlisted_tier_on_public_event in qs


# --- Additional Resource Visibility ---


class TestAdditionalResourceUnlistedVisibility:
    """Test that UNLISTED resources are accessible like PUBLIC ones."""

    @pytest.fixture
    def unlisted_resource(self, unlisted_org: Organization) -> AdditionalResource:
        return AdditionalResource.objects.create(
            organization=unlisted_org,
            name="Unlisted Resource",
            visibility=ResourceVisibility.UNLISTED,
            resource_type=AdditionalResource.ResourceTypes.LINK,
            link="https://example.com",
        )

    @pytest.fixture
    def public_resource(self, public_org: Organization) -> AdditionalResource:
        return AdditionalResource.objects.create(
            organization=public_org,
            name="Public Resource",
            visibility=ResourceVisibility.PUBLIC,
            resource_type=AdditionalResource.ResourceTypes.LINK,
            link="https://example.com",
        )

    def test_anonymous_sees_unlisted_resource(self, unlisted_resource: AdditionalResource) -> None:
        qs = AdditionalResource.objects.for_user(AnonymousUser())
        assert unlisted_resource in qs

    def test_outsider_sees_unlisted_resource(self, unlisted_resource: AdditionalResource, outsider: RevelUser) -> None:
        qs = AdditionalResource.objects.for_user(outsider)
        assert unlisted_resource in qs


# --- Address Visibility ---


class TestAddressVisibilityUnlisted:
    """Test that UNLISTED address_visibility is treated like PUBLIC."""

    def test_anonymous_can_see_unlisted_address(self, unlisted_event_on_public_org: Event) -> None:
        unlisted_event_on_public_org.address_visibility = ResourceVisibility.UNLISTED
        unlisted_event_on_public_org.address = "123 Test St"
        unlisted_event_on_public_org.save()
        assert unlisted_event_on_public_org.can_user_see_address(AnonymousUser()) is True

    def test_outsider_can_see_unlisted_address(self, unlisted_event_on_public_org: Event, outsider: RevelUser) -> None:
        unlisted_event_on_public_org.address_visibility = ResourceVisibility.UNLISTED
        unlisted_event_on_public_org.address = "123 Test St"
        unlisted_event_on_public_org.save()
        assert unlisted_event_on_public_org.can_user_see_address(outsider) is True
