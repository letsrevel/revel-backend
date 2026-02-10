"""Tests for the /version endpoint and maintenance banner logic."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from common.models import SiteSettings

pytestmark = pytest.mark.django_db

VERSION_URL = reverse("api:version")


class TestVersionEndpointBaseline:
    """Tests for /version without any banner configured."""

    def test_returns_version_and_demo(self, client: Client) -> None:
        """Test that /version returns the app version and demo flag."""
        response = client.get(VERSION_URL)
        data = response.json()

        assert response.status_code == 200
        assert data["version"] == settings.VERSION
        assert data["demo"] == settings.DEMO_MODE

    def test_banner_is_null_by_default(self, client: Client) -> None:
        """Test that banner is null when no maintenance message is set."""
        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is None


class TestVersionEndpointWithBanner:
    """Tests for /version when a maintenance banner is configured."""

    def test_banner_returned_when_message_set_no_end(self, client: Client) -> None:
        """Test that an open-ended banner (no ends_at) is returned."""
        site = SiteSettings.get_solo()
        site.maintenance_message = "Scheduled maintenance tonight"
        site.maintenance_severity = SiteSettings.BannerSeverity.WARNING
        site.save()

        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is not None
        assert data["banner"]["message"] == "Scheduled maintenance tonight"
        assert data["banner"]["severity"] == SiteSettings.BannerSeverity.WARNING.value
        assert data["banner"]["scheduled_at"] is None
        assert data["banner"]["ends_at"] is None

    def test_banner_returned_when_ends_at_in_future(self, client: Client) -> None:
        """Test that banner is returned when ends_at is in the future."""
        site = SiteSettings.get_solo()
        site.maintenance_message = "Brief downtime expected"
        site.maintenance_severity = SiteSettings.BannerSeverity.INFO
        site.maintenance_scheduled_at = timezone.now() + timedelta(hours=1)
        site.maintenance_ends_at = timezone.now() + timedelta(hours=3)
        site.save()

        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is not None
        assert data["banner"]["message"] == "Brief downtime expected"
        assert data["banner"]["severity"] == SiteSettings.BannerSeverity.INFO.value
        assert data["banner"]["scheduled_at"] is not None
        assert data["banner"]["ends_at"] is not None

    def test_banner_null_when_ends_at_in_past(self, client: Client) -> None:
        """Test that banner is hidden once the maintenance window has passed."""
        site = SiteSettings.get_solo()
        site.maintenance_message = "This maintenance is over"
        site.maintenance_severity = SiteSettings.BannerSeverity.ERROR
        site.maintenance_ends_at = timezone.now() - timedelta(hours=1)
        site.save()

        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is None

    def test_banner_null_when_message_is_blank(self, client: Client) -> None:
        """Test that banner is null when message is empty even if severity is set."""
        site = SiteSettings.get_solo()
        site.maintenance_message = ""
        site.maintenance_severity = SiteSettings.BannerSeverity.CRITICAL
        site.maintenance_ends_at = timezone.now() + timedelta(hours=5)
        site.save()

        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is None

    def test_banner_transitions_from_active_to_expired(self, client: Client) -> None:
        """Test that a banner becomes null once its ends_at passes."""
        ends_at = timezone.now() + timedelta(hours=1)

        site = SiteSettings.get_solo()
        site.maintenance_message = "Going down soon"
        site.maintenance_severity = SiteSettings.BannerSeverity.WARNING
        site.maintenance_ends_at = ends_at
        site.save()

        # Banner is active now
        response = client.get(VERSION_URL)
        assert response.json()["banner"] is not None

        # Jump past ends_at
        with freeze_time(ends_at + timedelta(minutes=1)):
            response = client.get(VERSION_URL)
            assert response.json()["banner"] is None

    @pytest.mark.parametrize(
        "severity",
        list(SiteSettings.BannerSeverity),
        ids=[s.value for s in SiteSettings.BannerSeverity],
    )
    def test_all_severity_levels_are_returned(self, client: Client, severity: SiteSettings.BannerSeverity) -> None:
        """Test that every valid severity level is correctly serialized."""
        site = SiteSettings.get_solo()
        site.maintenance_message = f"Testing {severity.value}"
        site.maintenance_severity = severity
        site.save()

        response = client.get(VERSION_URL)
        data = response.json()

        assert data["banner"] is not None
        assert data["banner"]["severity"] == severity.value
