"""Tests for UserAwareController (now in common.controllers.base) location functionality."""

import typing as t
from unittest.mock import Mock

import pytest
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import RequestFactory

from common.controllers import UserAwareController
from common.types import HttpRequest
from conftest import RevelUserFactory
from events.models import GeneralUserPreferences
from events.service.location_service import get_user_location_cache_key
from geo.ip2 import LazyGeoPoint
from geo.models import City

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_factory() -> RequestFactory:
    """Provide a Django request factory."""
    return RequestFactory()


@pytest.fixture
def controller() -> UserAwareController:
    """Create a UserAwareController instance."""
    return UserAwareController()


@pytest.fixture
def city() -> City:
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


class TestUserLocationMethod:
    def test_returns_city_preference_for_authenticated_user(
        self,
        controller: UserAwareController,
        revel_user_factory: RevelUserFactory,
        city: City,
        request_factory: RequestFactory,
    ) -> None:
        """Test that authenticated user's city preference is returned."""
        user = revel_user_factory()
        preferences = GeneralUserPreferences.objects.get(user=user)
        preferences.city = city
        preferences.save()

        # Create mock request with user and IP location
        request = t.cast(HttpRequest, request_factory.get("/"))
        request.user = user
        request.user_location = LazyGeoPoint("8.8.8.8")

        # Set up controller context
        controller.context = Mock(request=request)

        # Clear cache to ensure fresh fetch
        cache.delete(get_user_location_cache_key(user.id))

        # Get location
        location = controller.user_location()

        assert location is not None
        assert location.x == city.location.x
        assert location.y == city.location.y

    def test_falls_back_to_ip_location_when_no_city_preference(
        self, controller: UserAwareController, revel_user_factory: RevelUserFactory, request_factory: RequestFactory
    ) -> None:
        """Test that IP-based location is used when user has no city preference."""
        user = revel_user_factory()
        ip_location = Point(30.0, 40.0)

        # Create mock request
        request = t.cast(HttpRequest, request_factory.get("/"))
        request.user = user
        request.user_location = Mock()
        request.user_location.get = Mock(return_value=ip_location)

        # Set up controller context
        controller.context = Mock(request=request)

        # Clear cache
        cache.delete(get_user_location_cache_key(user.id))

        # Get location
        location = controller.user_location()

        assert location is not None
        assert location.x == ip_location.x
        assert location.y == ip_location.y

    def test_returns_ip_location_for_anonymous_user(
        self, controller: UserAwareController, request_factory: RequestFactory
    ) -> None:
        """Test that anonymous users get IP-based location without caching."""
        from django.contrib.auth.models import AnonymousUser

        ip_location = Point(50.0, 60.0)

        # Create mock request with anonymous user
        request = t.cast(HttpRequest, request_factory.get("/"))
        request.user = AnonymousUser()  # type: ignore[assignment]
        request.user_location = Mock()
        request.user_location.get = Mock(return_value=ip_location)

        # Set up controller context
        controller.context = Mock(request=request)

        # Get location
        location = controller.user_location()

        assert location is not None
        assert location.x == ip_location.x
        assert location.y == ip_location.y

        # Verify it was called (no caching for anonymous)
        request.user_location.get.assert_called_once()

    def test_uses_cached_location(
        self,
        controller: UserAwareController,
        revel_user_factory: RevelUserFactory,
        city: City,
        request_factory: RequestFactory,
    ) -> None:
        """Test that cached location is used on subsequent calls."""
        user = revel_user_factory()
        preferences = GeneralUserPreferences.objects.get(user=user)
        preferences.city = city
        preferences.save()

        # Create mock request
        request = t.cast(HttpRequest, request_factory.get("/"))
        request.user = user
        request.user_location = Mock()
        request.user_location.get = Mock(return_value=None)

        # Set up controller context
        controller.context = Mock(request=request)

        # Clear cache
        cache_key = get_user_location_cache_key(user.id)
        cache.delete(cache_key)

        # First call - should fetch from DB and cache
        location1 = controller.user_location()
        assert location1 is not None
        assert location1.x == city.location.x

        # Verify cache was set
        cached = cache.get(cache_key)
        assert cached is not None

        # Change city in DB (but cache should be used)
        new_city = City.objects.create(
            name="New City",
            ascii_name="New City",
            country="New Country",
            iso2="NC",
            iso3="NEW",
            city_id=67890,
            location=Point(70.0, 80.0),
            population=500000,
        )
        preferences.city = new_city
        preferences.save()

        # Second call - should use cache (old city)
        location2 = controller.user_location()
        assert location2 is not None
        assert location2.x == city.location.x  # Still old city from cache

    def test_returns_none_when_no_location_available(
        self, controller: UserAwareController, revel_user_factory: RevelUserFactory, request_factory: RequestFactory
    ) -> None:
        """Test that None is returned when no location is available."""
        user = revel_user_factory()

        # Create mock request with no IP location
        request = t.cast(HttpRequest, request_factory.get("/"))
        request.user = user
        request.user_location = Mock()
        request.user_location.get = Mock(return_value=None)

        # Set up controller context
        controller.context = Mock(request=request)

        # Clear cache
        cache.delete(get_user_location_cache_key(user.id))

        # Get location
        location = controller.user_location()

        assert location is None
