import typing as t

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


@pytest.fixture
def public_resource(organization: models.Organization) -> models.AdditionalResource:
    """A public resource."""
    return models.AdditionalResource.objects.create(
        organization=organization,
        name="Public Document",
        description="Visible to all",
        resource_type="text",
        text="Public content",
        visibility=models.AdditionalResource.Visibility.PUBLIC,
    )


@pytest.fixture
def member_resource(organization: models.Organization) -> models.AdditionalResource:
    """A members-only resource."""
    return models.AdditionalResource.objects.create(
        organization=organization,
        name="Member Handbook",
        description="For members only",
        resource_type="text",
        text="Secret member content",
        visibility=models.AdditionalResource.Visibility.MEMBERS_ONLY,
    )


@pytest.fixture
def private_resource_for_event(
    organization: models.Organization, private_event: models.Event
) -> models.AdditionalResource:
    """A private resource linked to a specific event."""
    resource = models.AdditionalResource.objects.create(
        organization=organization,
        name="Private Event Info",
        resource_type="text",
        text="Top secret event details",
        visibility=models.AdditionalResource.Visibility.PRIVATE,
    )
    resource.events.add(private_event)
    return resource


class TestPublicResourceEndpoints:
    def test_list_organization_resources_visibility(
        self,
        client: Client,
        member_client: Client,
        organization: models.Organization,
        public_resource: models.AdditionalResource,
        member_resource: models.AdditionalResource,
    ) -> None:
        """Test that resource visibility is correctly applied for different user types."""
        organization.visibility = models.AdditionalResource.Visibility.PUBLIC
        organization.save()
        url = reverse("api:list_organization_resources", kwargs={"slug": organization.slug})

        # Anonymous user sees only the public resource
        anon_response = client.get(url)
        assert anon_response.status_code == 200
        assert anon_response.json()["count"] == 1
        assert anon_response.json()["results"][0]["name"] == "Public Document"

        # Member sees both public and members-only resources
        member_response = member_client.get(url)
        assert member_response.status_code == 200
        assert member_response.json()["count"] == 2

    def test_list_private_resource_with_ticket(
        self,
        nonmember_client: Client,
        nonmember_user: RevelUser,
        private_event: models.Event,
        private_resource_for_event: models.AdditionalResource,
    ) -> None:
        """Test that a user with a ticket for an event can see a private resource linked to it."""
        # Give the user a ticket to the event
        tier = private_event.ticket_tiers.first()
        assert tier is not None
        models.Ticket.objects.create(user=nonmember_user, event=private_event, tier=tier)
        url = reverse("api:list_event_resources", kwargs={"event_id": private_event.id})

        response = nonmember_client.get(url)
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["name"] == "Private Event Info"

    def test_list_resources_search_and_filter(
        self,
        client: Client,
        organization: models.Organization,
        public_resource: models.AdditionalResource,
        member_resource: models.AdditionalResource,
    ) -> None:
        """Test searching and filtering resources."""
        organization.visibility = models.AdditionalResource.Visibility.PUBLIC
        organization.save()
        url = reverse("api:list_organization_resources", kwargs={"slug": organization.slug})

        # Search by name
        response = client.get(url, {"search": "Public"})
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["name"] == "Public Document"

        # Filter by resource_type
        # Add another resource to test filtering
        models.AdditionalResource.objects.create(
            organization=organization,
            name="Public Link",
            resource_type="link",
            link="https://a.com",
            visibility=models.AdditionalResource.Visibility.PUBLIC,
        )
        response = client.get(url, {"resource_type": "text"})
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["name"] == "Public Document"


class TestAdminResourceEndpoints:
    @pytest.fixture
    def create_payload(self, event: models.Event, event_series: models.EventSeries) -> dict[str, t.Any]:
        return {
            "name": "Admin Created Resource",
            "description": "Created via API",
            "resource_type": "link",
            "link": "https://admin.example.com",
            "visibility": "staff-only",
            "event_ids": [str(event.id)],
            "event_series_ids": [str(event_series.id)],
        }

    def test_list_resources_admin(
        self,
        organization_owner_client: Client,
        organization: models.Organization,
        public_resource: models.AdditionalResource,
    ) -> None:
        """Test that an admin can list all resources for an organization, regardless of visibility."""
        url = reverse("api:list_organization_resources_admin", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        assert response.json()["count"] > 0

    def test_create_resource_admin(
        self, organization_owner_client: Client, organization: models.Organization, create_payload: dict[str, t.Any]
    ) -> None:
        """Test that an admin can create a resource with M2M links."""
        url = reverse("api:create_organization_resource", kwargs={"slug": organization.slug})
        response = organization_owner_client.post(
            url, data=orjson.dumps(create_payload), content_type="application/json"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Admin Created Resource"

        resource = models.AdditionalResource.objects.get(id=data["id"])
        assert resource.events.count() == 1
        assert resource.event_series.count() == 1

    def test_create_resource_with_invalid_m2m_fails(
        self,
        organization_owner_client: Client,
        organization: models.Organization,
        create_payload: dict[str, t.Any],
    ) -> None:
        """Test that creating a resource with an M2M link to another org's event fails."""
        another_organization = models.Organization.objects.create(
            owner=organization.owner, name="Anonymous", slug="anon"
        )
        another_event = models.Event.objects.create(
            organization=another_organization, name="Anonymous", slug="anon", start=timezone.now()
        )
        url = reverse("api:create_organization_resource", kwargs={"slug": organization.slug})
        create_payload["event_ids"] = [str(another_event.id)]  # Invalid event
        response = organization_owner_client.post(
            url, data=orjson.dumps(create_payload), content_type="application/json"
        )
        assert response.status_code == 400
        assert "events do not exist or belong to this organization" in response.json()["detail"]

    def test_update_resource_admin(
        self,
        organization_owner_client: Client,
        organization: models.Organization,
        public_resource: models.AdditionalResource,
    ) -> None:
        """Test that an admin can update a resource."""
        url = reverse(
            "api:update_organization_resource", kwargs={"slug": organization.slug, "resource_id": public_resource.id}
        )
        payload = {"name": "Updated by Admin", "event_ids": []}  # Update name and remove M2M links
        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        public_resource.refresh_from_db()
        assert public_resource.name == "Updated by Admin"
        assert public_resource.events.count() == 0

    def test_delete_resource_admin(
        self,
        organization_owner_client: Client,
        organization: models.Organization,
        public_resource: models.AdditionalResource,
    ) -> None:
        """Test that an admin can delete a resource."""
        url = reverse(
            "api:delete_organization_resource", kwargs={"slug": organization.slug, "resource_id": public_resource.id}
        )
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not models.AdditionalResource.objects.filter(id=public_resource.id).exists()

    def test_non_admin_cannot_create_resource(
        self, member_client: Client, organization: models.Organization, create_payload: dict[str, t.Any]
    ) -> None:
        """Test that a non-admin (e.g., a member) receives a 403 trying to access admin endpoints."""
        url = reverse("api:create_organization_resource", kwargs={"slug": organization.slug})
        response = member_client.post(url, data=orjson.dumps(create_payload), content_type="application/json")
        assert response.status_code == 403
