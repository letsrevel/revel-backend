import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Event, EventSeries, Organization, OrganizationStaff

pytestmark = pytest.mark.django_db


class TestOrganizationTagEndpoints:
    """Tests for tag management on the OrganizationAdminController."""

    def test_add_remove_clear_tags_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that the organization owner can add, remove, and clear tags."""
        add_url = reverse("api:add_organization_tags", kwargs={"slug": organization.slug})
        remove_url = reverse("api:remove_organization_tags", kwargs={"slug": organization.slug})
        clear_url = reverse("api:clear_organization_tags", kwargs={"slug": organization.slug})

        # Add tags
        add_payload = {"tags": ["Community", "Official Event "]}  # Note trailing space
        response_add = organization_owner_client.post(
            add_url, data=orjson.dumps(add_payload), content_type="application/json"
        )
        assert response_add.status_code == 200
        tags = {tag["name"] for tag in response_add.json()}
        assert tags == {"Community", "Official Event"}

        organization.refresh_from_db()
        assert {tag.name for tag in organization.tags_manager.all()} == {"Community", "Official Event"}

        # Remove a specific tag
        remove_payload = {"tags": ["Community"]}
        response_remove = organization_owner_client.post(
            remove_url, data=orjson.dumps(remove_payload), content_type="application/json"
        )
        assert response_remove.status_code == 200
        tags = {tag["name"] for tag in response_remove.json()}
        assert tags == {"Official Event"}
        organization.refresh_from_db()
        assert {tag.name for tag in organization.tags_manager.all()} == {"Official Event"}

        # Clear all tags
        response_clear = organization_owner_client.delete(clear_url)
        assert response_clear.status_code == 204
        organization.refresh_from_db()
        assert organization.tags.count() == 0

    def test_add_tags_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test staff with 'edit_organization' permission can add tags."""
        # Permission is granted in the fixture
        url = reverse("api:add_organization_tags", kwargs={"slug": organization.slug})
        payload = {"tags": ["Staff Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        assert "Staff Tag" in [tag["name"] for tag in response.json()]

    def test_add_tags_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test staff without 'edit_organization' permission gets 403."""
        # Revoke permission
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:add_organization_tags", kwargs={"slug": organization.slug})
        payload = {"tags": ["Forbidden Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "client_fixture, expected_status",
        [
            ("member_client", 403),
            ("nonmember_client", 404),
            ("client", 401),
        ],
    )
    def test_add_tags_by_unauthorized_users(
        self, request: pytest.FixtureRequest, client_fixture: str, expected_status: int, organization: Organization
    ) -> None:
        """Test unauthorized users cannot add tags."""
        client: Client = request.getfixturevalue(client_fixture)
        url = reverse("api:add_organization_tags", kwargs={"slug": organization.slug})
        payload = {"tags": ["Unauthorized Tag"]}
        response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == expected_status


class TestEventTagEndpoints:
    """Tests for tag management on the EventAdminController."""

    def test_add_remove_clear_tags_by_owner(self, organization_owner_client: Client, event: Event) -> None:
        """Test that the event's organization owner can add, remove, and clear tags."""
        add_url = reverse("api:add_event_tags", kwargs={"event_id": event.pk})
        remove_url = reverse("api:remove_event_tags", kwargs={"event_id": event.pk})
        clear_url = reverse("api:clear_event_tags", kwargs={"event_id": event.pk})

        # Add tags
        add_payload = {"tags": ["Party", "Music"]}
        response_add = organization_owner_client.post(
            add_url, data=orjson.dumps(add_payload), content_type="application/json"
        )
        assert response_add.status_code == 200
        assert {tag["name"] for tag in response_add.json()} == {"Party", "Music"}
        event.refresh_from_db()
        assert {tag.name for tag in event.tags_manager.all()} == {"Party", "Music"}

        # Remove a tag
        remove_payload = {"tags": ["Party"]}
        response_remove = organization_owner_client.post(
            remove_url, data=orjson.dumps(remove_payload), content_type="application/json"
        )
        assert response_remove.status_code == 200
        assert {tag["name"] for tag in response_remove.json()} == {"Music"}
        event.refresh_from_db()
        assert {tag.name for tag in event.tags_manager.all()} == {"Music"}

        # Clear all tags
        response_clear = organization_owner_client.delete(clear_url)
        assert response_clear.status_code == 204
        event.refresh_from_db()
        assert event.tags.count() == 0

    def test_add_tags_by_staff_with_permission(
        self, organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
    ) -> None:
        """Test staff with 'edit_event' permission can add tags."""
        # Default permission map gives edit_event=True
        url = reverse("api:add_event_tags", kwargs={"event_id": event.pk})
        payload = {"tags": ["Staff Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        assert "Staff Tag" in [tag["name"] for tag in response.json()]

    def test_add_tags_by_staff_without_permission(
        self, organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
    ) -> None:
        """Test staff without 'edit_event' permission gets 403."""
        # Revoke permission
        perms = staff_member.permissions
        perms["default"]["edit_event"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:add_event_tags", kwargs={"event_id": event.pk})
        payload = {"tags": ["Forbidden Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403


class TestEventSeriesTagEndpoints:
    """Tests for tag management on the EventSeriesAdminController."""

    def test_add_remove_clear_tags_by_owner(self, organization_owner_client: Client, event_series: EventSeries) -> None:
        """Test that the event series' organization owner can add, remove, and clear tags."""
        add_url = reverse("api:add_event_series_tags", kwargs={"series_id": event_series.pk})
        remove_url = reverse("api:remove_event_series_tags", kwargs={"series_id": event_series.pk})
        clear_url = reverse("api:clear_event_series_tags", kwargs={"series_id": event_series.pk})

        # Add tags
        add_payload = {"tags": ["Festival", "Annual"]}
        response_add = organization_owner_client.post(
            add_url, data=orjson.dumps(add_payload), content_type="application/json"
        )
        assert response_add.status_code == 200
        assert {tag["name"] for tag in response_add.json()} == {"Festival", "Annual"}
        event_series.refresh_from_db()
        assert {tag.name for tag in event_series.tags_manager.all()} == {"Festival", "Annual"}

        # Remove a tag
        remove_payload = {"tags": ["Festival"]}
        response_remove = organization_owner_client.post(
            remove_url, data=orjson.dumps(remove_payload), content_type="application/json"
        )
        assert response_remove.status_code == 200
        assert {tag["name"] for tag in response_remove.json()} == {"Annual"}
        event_series.refresh_from_db()
        assert {tag.name for tag in event_series.tags_manager.all()} == {"Annual"}

        # Clear all tags
        response_clear = organization_owner_client.delete(clear_url)
        assert response_clear.status_code == 204
        event_series.refresh_from_db()
        assert event_series.tags.count() == 0

    def test_add_tags_by_staff_with_permission(
        self, organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
    ) -> None:
        """Test staff with 'edit_event_series' permission can add tags."""
        perms = staff_member.permissions
        perms["default"]["edit_event_series"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:add_event_series_tags", kwargs={"series_id": event_series.pk})
        payload = {"tags": ["Staff Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        assert "Staff Tag" in [tag["name"] for tag in response.json()]

    def test_add_tags_by_staff_without_permission(
        self, organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
    ) -> None:
        """Test staff without 'edit_event_series' permission gets 403."""
        # It's false by default, but let's be explicit
        perms = staff_member.permissions
        perms["default"]["edit_event_series"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:add_event_series_tags", kwargs={"series_id": event_series.pk})
        payload = {"tags": ["Forbidden Tag"]}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403
