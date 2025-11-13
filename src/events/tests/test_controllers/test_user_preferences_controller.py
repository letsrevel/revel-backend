"""Tests for user preferences controller."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


class TestUserPreferencesController:
    """Test user preferences controller endpoints."""

    def test_get_general_preferences_creates_defaults(self, member_client: Client) -> None:
        """Test that getting global prefs for a user for the first time creates them."""
        url = reverse("api:get_general_preferences")
        response = member_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["show_me_on_attendee_list"] == "never"  # Default value

    def test_update_general_preferences(self, member_client: Client, member_user: RevelUser) -> None:
        """Test updating global preferences."""
        url = reverse("api:update_general_preferences")
        payload = {"show_me_on_attendee_list": "always"}
        response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["show_me_on_attendee_list"] == "always"

        prefs = models.GeneralUserPreferences.objects.get(user=member_user)
        assert prefs.show_me_on_attendee_list == "always"
