"""Tests for user preferences service."""

import pytest
from django.contrib.gis.geos import Point
from django.core.cache import cache

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events import models
from events.models import OrganizationMember, OrganizationStaff
from events.schema import GeneralUserPreferencesUpdateSchema
from events.service.location_service import get_user_location_cache_key
from events.service.user_preferences_service import resolve_visibility, set_general_preferences
from geo.models import City

pytestmark = pytest.mark.django_db


@pytest.fixture
def viewer(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


@pytest.fixture
def target(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


class TestResolveVisibility:
    """Test visibility resolution based on user preferences."""

    def test_owner_can_always_see(
        self, organization_owner_user: RevelUser, target: RevelUser, event: models.Event
    ) -> None:
        """Test that the organization owner can see any attendee."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=organization_owner_user,
                target=target,
                event=event,
                owner_id=organization_owner_user.id,
                staff_ids=set(),
            )
            is True
        )

    def test_staff_can_always_see(
        self,
        organization_staff_user: RevelUser,
        target: RevelUser,
        event: models.Event,
        staff_member: OrganizationStaff,
    ) -> None:
        """Test that a staff member can see any attendee."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=organization_staff_user,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids={organization_staff_user.id},
            )
            is True
        )

    def test_target_preference_always(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'always' preference makes the target visible."""
        target.general_preferences.show_me_on_attendee_list = "always"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

    def test_target_preference_never(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'never' preference makes the target invisible."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )

    def test_preference_to_members(
        self, viewer: RevelUser, target: RevelUser, event: models.Event, organization_membership: OrganizationMember
    ) -> None:
        """Test 'to_members' preference visibility."""
        target.general_preferences.show_me_on_attendee_list = "to_members"
        target.general_preferences.save()

        # Both are members
        models.OrganizationMember.objects.create(organization=event.organization, user=viewer)
        models.OrganizationMember.objects.create(organization=event.organization, user=target)
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

        # Viewer is not a member
        viewer.organization_memberships.all().delete()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )

    def test_preference_to_invitees(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'to_invitees' preference visibility."""
        target.general_preferences.show_me_on_attendee_list = "to_invitees"
        target.general_preferences.save()

        # Viewer is an attendee (via ticket)
        tier = event.ticket_tiers.first()
        assert tier is not None
        models.Ticket.objects.create(event=event, user=viewer, tier=tier)
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

        # Viewer is not an attendee
        viewer.tickets.all().delete()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )


class TestSetGeneralPreferences:
    """Test set_general_preferences function."""

    def test_invalidates_location_cache_when_city_changes(self, member_user: RevelUser) -> None:
        """Test that location cache is invalidated when city preference changes."""
        # Create test cities
        city1 = City.objects.create(
            name="City One",
            ascii_name="City One",
            country="Country",
            iso2="C1",
            iso3="CY1",
            city_id=11111,
            location=Point(10.0, 20.0),
            population=1000000,
        )
        city2 = City.objects.create(
            name="City Two",
            ascii_name="City Two",
            country="Country",
            iso2="C2",
            iso3="CY2",
            city_id=22222,
            location=Point(30.0, 40.0),
            population=2000000,
        )

        # Set initial city
        member_user.general_preferences.city = city1
        member_user.general_preferences.save()

        # Set cache
        cache_key = get_user_location_cache_key(member_user.id)
        cache.set(cache_key, "some_cached_location", timeout=3600)
        assert cache.get(cache_key) is not None

        # Update city via set_general_preferences
        payload = GeneralUserPreferencesUpdateSchema(city_id=city2.id)
        set_general_preferences(member_user.general_preferences, payload)

        # Cache should be invalidated
        assert cache.get(cache_key) is None

    def test_does_not_invalidate_cache_when_city_unchanged(self, member_user: RevelUser) -> None:
        """Test that location cache is NOT invalidated when city doesn't change."""
        # Create city
        city = City.objects.create(
            name="Test City",
            ascii_name="Test City",
            country="Test Country",
            iso2="TC",
            iso3="TST",
            city_id=33333,
            location=Point(50.0, 60.0),
            population=500000,
        )

        # Set city
        member_user.general_preferences.city = city
        member_user.general_preferences.save()

        # Set cache
        cache_key = get_user_location_cache_key(member_user.id)
        cache.set(cache_key, "some_cached_location", timeout=3600)
        assert cache.get(cache_key) is not None

        # Update other preference (not city)
        payload = GeneralUserPreferencesUpdateSchema(
            show_me_on_attendee_list=models.GeneralUserPreferences.VisibilityPreference.ALWAYS
        )
        set_general_preferences(member_user.general_preferences, payload)

        # Cache should still exist
        assert cache.get(cache_key) is not None

    def test_invalidates_cache_when_city_set_to_null(self, member_user: RevelUser) -> None:
        """Test that location cache is invalidated when city is removed."""
        # Create and set city
        city = City.objects.create(
            name="Remove City",
            ascii_name="Remove City",
            country="Test",
            iso2="RC",
            iso3="RMC",
            city_id=44444,
            location=Point(70.0, 80.0),
            population=300000,
        )
        member_user.general_preferences.city = city
        member_user.general_preferences.save()

        # Set cache
        cache_key = get_user_location_cache_key(member_user.id)
        cache.set(cache_key, "some_cached_location", timeout=3600)
        assert cache.get(cache_key) is not None

        # Remove city
        payload = GeneralUserPreferencesUpdateSchema(city_id=None)
        set_general_preferences(member_user.general_preferences, payload)

        # Cache should be invalidated
        assert cache.get(cache_key) is None
