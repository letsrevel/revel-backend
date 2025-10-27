"""Tests for location caching service."""

import pytest
from django.contrib.gis.geos import Point
from django.core.cache import cache

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import GeneralUserPreferences
from events.service.location_service import (
    get_cached_user_location,
    get_user_location_cache_key,
    get_user_location_from_preferences,
    invalidate_user_location_cache,
)
from geo.models import City

pytestmark = pytest.mark.django_db


@pytest.fixture
def city(db: None) -> City:
    """Create a test city."""
    return City.objects.create(
        name="Test City",
        ascii_name="Test City",
        country="Test Country",
        iso2="TC",
        iso3="TST",
        city_id=12345,
        location=Point(10.0, 20.0),
        population=1000000,
    )


@pytest.fixture
def user_with_city(revel_user_factory: RevelUserFactory, city: City) -> RevelUser:
    """Create a user with a city preference set."""
    user = revel_user_factory()
    preferences = GeneralUserPreferences.objects.get(user=user)
    preferences.city = city
    preferences.save()
    return user


class TestGetUserLocationCacheKey:
    def test_generates_consistent_key(self) -> None:
        """Test that cache key generation is consistent."""
        user_id = 123
        key1 = get_user_location_cache_key(user_id)
        key2 = get_user_location_cache_key(user_id)
        assert key1 == key2
        assert key1 == "user_location:123"

    def test_handles_different_types(self) -> None:
        """Test that cache key generation handles different ID types."""
        from uuid import uuid4

        user_uuid = uuid4()
        key1 = get_user_location_cache_key(user_uuid)
        key2 = get_user_location_cache_key(str(user_uuid))
        assert key1 == key2


class TestGetUserLocationFromPreferences:
    def test_returns_city_location_when_set(self, user_with_city: RevelUser, city: City) -> None:
        """Test that user's city location is returned when set."""
        location = get_user_location_from_preferences(user_with_city)
        assert location is not None
        assert location.x == city.location.x
        assert location.y == city.location.y

    def test_returns_none_when_no_city_set(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that None is returned when user has no city preference."""
        user = revel_user_factory()
        location = get_user_location_from_preferences(user)
        assert location is None

    def test_returns_none_when_preferences_dont_exist(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that None is returned when user has no preferences object."""
        user = revel_user_factory()
        # Delete preferences
        GeneralUserPreferences.objects.filter(user=user).delete()
        location = get_user_location_from_preferences(user)
        assert location is None


class TestInvalidateUserLocationCache:
    def test_invalidates_existing_cache(self, user_with_city: RevelUser) -> None:
        """Test that cache is properly invalidated."""
        # Set cache
        cache_key = get_user_location_cache_key(user_with_city.id)
        cache.set(cache_key, Point(1.0, 2.0), timeout=3600)
        assert cache.get(cache_key) is not None

        # Invalidate
        invalidate_user_location_cache(user_with_city.id)
        assert cache.get(cache_key) is None

    def test_handles_non_existent_cache(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that invalidating non-existent cache doesn't raise errors."""
        user = revel_user_factory()
        # Should not raise
        invalidate_user_location_cache(user.id)


class TestGetCachedUserLocation:
    def test_caches_location_from_preferences(self, user_with_city: RevelUser, city: City) -> None:
        """Test that location from preferences is cached."""
        cache_key = get_user_location_cache_key(user_with_city.id)

        # Ensure cache is empty
        cache.delete(cache_key)
        assert cache.get(cache_key) is None

        # First call should set cache
        location = get_cached_user_location(user_with_city)
        assert location is not None
        assert location.x == city.location.x
        assert location.y == city.location.y

        # Verify cache was set
        cached = cache.get(cache_key)
        assert cached is not None
        assert cached.x == city.location.x
        assert cached.y == city.location.y

    def test_returns_cached_value_on_subsequent_calls(self, user_with_city: RevelUser, city: City) -> None:
        """Test that cached value is returned without DB query."""
        cache_key = get_user_location_cache_key(user_with_city.id)
        cache.delete(cache_key)

        # First call
        location1 = get_cached_user_location(user_with_city)

        # Change the city in DB (but cache should still be used)
        preferences = GeneralUserPreferences.objects.get(user=user_with_city)
        new_city = City.objects.create(
            name="New City",
            ascii_name="New City",
            country="New Country",
            iso2="NC",
            iso3="NEW",
            city_id=67890,
            location=Point(30.0, 40.0),
            population=500000,
        )
        preferences.city = new_city
        preferences.save()

        # Second call should still return cached location (old city)
        location2 = get_cached_user_location(user_with_city)
        assert location1 == location2
        assert location2 is not None
        assert location2.x == city.location.x  # Still the old city
        assert location2.y == city.location.y

    def test_uses_fallback_when_no_preference(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that fallback location is used when no city preference is set."""
        user = revel_user_factory()
        fallback = Point(50.0, 60.0)

        location = get_cached_user_location(user, fallback_location=fallback)
        assert location is not None
        assert location.x == fallback.x
        assert location.y == fallback.y

    def test_caches_fallback_location(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that fallback location is also cached."""
        user = revel_user_factory()
        fallback = Point(50.0, 60.0)
        cache_key = get_user_location_cache_key(user.id)
        cache.delete(cache_key)

        # First call with fallback
        location1 = get_cached_user_location(user, fallback_location=fallback)
        assert location1 == fallback

        # Second call without fallback should still return cached fallback
        location2 = get_cached_user_location(user, fallback_location=None)
        assert location2 is not None
        assert location2.x == fallback.x
        assert location2.y == fallback.y

    def test_returns_none_when_no_preference_and_no_fallback(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that None is returned when there's no preference and no fallback."""
        user = revel_user_factory()
        cache_key = get_user_location_cache_key(user.id)
        cache.delete(cache_key)

        location = get_cached_user_location(user, fallback_location=None)
        assert location is None

    def test_cache_respects_timeout(self, user_with_city: RevelUser) -> None:
        """Test that cache entries have the correct timeout."""
        cache_key = get_user_location_cache_key(user_with_city.id)
        cache.delete(cache_key)

        # Call to set cache
        get_cached_user_location(user_with_city)

        # Check cache exists
        cached = cache.get(cache_key)
        assert cached is not None

        # TTL should be close to 3600 seconds (1 hour)
        # Note: Not all cache backends support TTL inspection, so this is a best-effort test
        ttl = cache.ttl(cache_key) if hasattr(cache, "ttl") else None
        if ttl is not None:
            assert 3500 < ttl <= 3600  # Allow some time for execution


class TestCacheInvalidationIntegration:
    def test_fresh_location_after_cache_invalidation(self, user_with_city: RevelUser, city: City) -> None:
        """Test that fresh location is fetched after cache invalidation."""
        cache_key = get_user_location_cache_key(user_with_city.id)
        cache.delete(cache_key)

        # First call - caches old city
        location1 = get_cached_user_location(user_with_city)
        assert location1 is not None
        assert location1.x == city.location.x
        assert location1.y == city.location.y

        # Change city
        new_city = City.objects.create(
            name="Another City",
            ascii_name="Another City",
            country="Another Country",
            iso2="AC",
            iso3="ANO",
            city_id=99999,
            location=Point(70.0, 80.0),
            population=2000000,
        )
        preferences = GeneralUserPreferences.objects.get(user=user_with_city)
        preferences.city = new_city
        preferences.save()

        # Without invalidation, still returns cached old city
        location2 = get_cached_user_location(user_with_city)
        assert location2 is not None
        assert location2.x == city.location.x  # Still old

        # Invalidate cache
        invalidate_user_location_cache(user_with_city.id)

        # Now should return new city
        location3 = get_cached_user_location(user_with_city)
        assert location3 is not None
        assert location3.x == new_city.location.x
        assert location3.y == new_city.location.y
