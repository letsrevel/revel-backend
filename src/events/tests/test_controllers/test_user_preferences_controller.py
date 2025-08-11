import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


class TestUserPreferencesController:
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
        payload = {"show_me_on_attendee_list": "always", "event_reminders": False}
        response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["show_me_on_attendee_list"] == "always"
        assert data["event_reminders"] is False

        prefs = models.GeneralUserPreferences.objects.get(user=member_user)
        assert prefs.show_me_on_attendee_list == "always"

    def test_get_organization_preferences(
        self, member_user: RevelUser, member_client: Client, organization: models.Organization
    ) -> None:
        """Test getting organization-specific preferences."""
        models.UserOrganizationPreferences.objects.create(user=member_user, organization=organization)
        url = reverse("api:get_organization_preferences", kwargs={"organization_id": organization.id})
        response = member_client.get(url)
        assert response.status_code == 200, response.content
        assert models.UserOrganizationPreferences.objects.count() == 1

    def test_update_organization_preferences(self, member_client: Client, organization: models.Organization) -> None:
        """Test updating organization-specific preferences."""
        url = reverse("api:update_organization_preferences", kwargs={"organization_id": organization.id})
        payload = {"is_subscribed": True, "notify_on_new_events": False}
        response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["is_subscribed"] is True
        assert data["notify_on_new_events"] is False

    def test_get_event_series_preferences(
        self, member_user: RevelUser, member_client: Client, event_series: models.EventSeries
    ) -> None:
        """Test getting event series-specific preferences."""
        models.UserEventSeriesPreferences.objects.create(user=member_user, event_series=event_series)
        url = reverse("api:get_event_series_preferences", kwargs={"series_id": event_series.id})
        response = member_client.get(url)
        assert response.status_code == 200, response.content
        assert models.UserEventSeriesPreferences.objects.count() == 1

    def test_update_event_series_preferences(self, member_client: Client, event_series: models.EventSeries) -> None:
        """Test updating event series-specific preferences."""
        url = reverse("api:update_event_series_preferences", kwargs={"series_id": event_series.id})
        payload = {"show_me_on_attendee_list": "to_members"}
        response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200, response.content
        assert response.json()["show_me_on_attendee_list"] == "to_members"

    def test_get_event_preferences(self, member_user: RevelUser, member_client: Client, event: models.Event) -> None:
        """Test getting event-specific preferences."""
        models.UserEventPreferences.objects.create(user=member_user, event=event)
        event.visibility = models.Event.Visibility.PUBLIC
        event.save()
        url = reverse("api:get_event_preferences", kwargs={"event_id": event.id})
        response = member_client.get(url)
        assert response.status_code == 200, response.content
        assert models.UserEventPreferences.objects.count() == 1

    def test_update_event_preferences(self, member_client: Client, event: models.Event) -> None:
        """Test updating event-specific preferences."""
        event.visibility = models.Event.Visibility.PUBLIC
        event.save()
        url = reverse("api:update_event_preferences", kwargs={"event_id": event.id})
        payload = {"notify_on_potluck_updates": True}
        response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200, response.content
        assert response.json()["notify_on_potluck_updates"] is True

    def test_unauthorized_access_to_prefs(self, nonmember_client: Client, organization: models.Organization) -> None:
        """Test that a user cannot access preferences for an org they cannot see."""
        organization.visibility = models.Organization.Visibility.PRIVATE
        organization.save()
        url = reverse("api:get_organization_preferences", kwargs={"organization_id": organization.id})
        response = nonmember_client.get(url)
        assert response.status_code == 404
