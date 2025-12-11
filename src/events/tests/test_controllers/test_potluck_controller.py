import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events import models
from events.models import Event, OrganizationStaff, PotluckItem

pytestmark = pytest.mark.django_db


class TestPotluckController:
    def test_list_potluck_items(self, organization_owner_client: Client, event: Event) -> None:
        """Test that potluck items for an event can be listed."""
        PotluckItem.objects.create(event=event, name="Chips", item_type="food")
        PotluckItem.objects.create(event=event, name="Salsa", item_type="food")
        url = reverse("api:list_potluck_items", kwargs={"event_id": event.id})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_create_potluck_item_by_owner(self, organization_owner_client: Client, event: Event) -> None:
        """Test that an event owner can create a potluck item."""
        url = reverse("api:create_potluck_item", kwargs={"event_id": event.id})
        payload = {"name": "Salad", "item_type": "food"}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        assert PotluckItem.objects.filter(event=event, name="Salad").exists()

    def test_update_potluck_item_by_creator(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that the creator of a potluck item can update it."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(
            event=event, name="Drinks", item_type="drink", created_by=nonmember_user
        )
        url = reverse("api:update_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        payload = {"name": "Juice", "item_type": "drink"}
        response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        potluck_item.refresh_from_db()
        assert potluck_item.name == "Juice"

    def test_delete_potluck_item_by_creator(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that the creator of a potluck item can delete it."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(
            event=event, name="Dessert", item_type="food", created_by=nonmember_user
        )
        url = reverse("api:delete_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = nonmember_client.delete(url)
        assert response.status_code == 204
        assert not PotluckItem.objects.filter(id=potluck_item.id).exists()

    def test_claim_potluck_item(self, nonmember_client: Client, event: Event, nonmember_user: RevelUser) -> None:
        """Test that a user can claim a potluck item."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(event=event, name="Napkins", item_type="supplies", is_suggested=True)
        url = reverse("api:claim_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        potluck_item.refresh_from_db()
        assert potluck_item.assignee == nonmember_user

    def test_unclaim_potluck_item(self, nonmember_client: Client, event: Event, nonmember_user: RevelUser) -> None:
        """Test that a user can unclaim a potluck item they previously claimed."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(
            event=event, name="Paper Towels", item_type="supplies", is_suggested=True, assignee=nonmember_user
        )
        url = reverse("api:unclaim_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        potluck_item.refresh_from_db()
        assert potluck_item.assignee is None


class TestPotluckControllerPermissions:
    def test_update_potluck_item_by_owner(self, organization_owner_client: Client, event: Event) -> None:
        """Test that an event owner can update a potluck item."""
        potluck_item = PotluckItem.objects.create(event=event, name="Plates", item_type="supplies")
        url = reverse("api:update_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        payload = {"name": "Paper Plates", "item_type": "supplies"}
        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        potluck_item.refresh_from_db()
        assert potluck_item.name == "Paper Plates"

    def test_delete_potluck_item_by_owner(self, organization_owner_client: Client, event: Event) -> None:
        """Test that an event owner can delete a potluck item."""
        potluck_item = PotluckItem.objects.create(event=event, name="Cups", item_type="supplies")
        url = reverse("api:delete_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not PotluckItem.objects.filter(id=potluck_item.id).exists()

    def test_update_potluck_item_by_staff(
        self, organization_staff_client: Client, event: Event, organization_staff_user: RevelUser
    ) -> None:
        """Test that a staff member can update a potluck item."""
        staff_membership = OrganizationStaff.objects.get(user=organization_staff_user)
        staff_membership.permissions["default"]["manage_potluck"] = True
        staff_membership.save()
        potluck_item = PotluckItem.objects.create(event=event, name="Ice", item_type="supplies")
        url = reverse("api:update_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        payload = {"name": "Crushed Ice", "item_type": "supplies"}
        response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        potluck_item.refresh_from_db()
        assert potluck_item.name == "Crushed Ice"

    def test_delete_potluck_item_by_staff(
        self, organization_staff_client: Client, event: Event, organization_staff_user: RevelUser
    ) -> None:
        """Test that a staff member can delete a potluck item."""
        staff_membership = OrganizationStaff.objects.get(user=organization_staff_user)
        staff_membership.permissions["default"]["manage_potluck"] = True
        staff_membership.save()
        potluck_item = PotluckItem.objects.create(event=event, name="Spoons", item_type="supplies")
        url = reverse("api:delete_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = organization_staff_client.delete(url)
        assert response.status_code == 204
        assert not PotluckItem.objects.filter(id=potluck_item.id).exists()

    def test_update_potluck_item_by_non_creator_non_staff(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that a non-creator, non-staff member cannot update a potluck item."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(event=event, name="Forks", item_type="supplies")
        url = reverse("api:update_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        payload = {"name": "Plastic Forks", "item_type": "supplies"}
        response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403

    def test_delete_potluck_item_by_non_creator_non_staff(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that a non-creator, non-staff member cannot delete a potluck item."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        potluck_item = PotluckItem.objects.create(event=event, name="Knives", item_type="supplies")
        url = reverse("api:delete_potluck_item", kwargs={"event_id": event.id, "item_id": potluck_item.id})
        response = nonmember_client.delete(url)
        assert response.status_code == 403


class TestCreatePotluckItemPermissions:
    def test_create_potluck_item_by_staff(
        self,
        organization_staff_client: Client,
        event: Event,
    ) -> None:
        """Test that a staff member with permission can create a potluck item."""
        url = reverse("api:create_potluck_item", kwargs={"event_id": event.id})
        payload = {"name": "Salad", "item_type": "food"}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200

    def test_create_potluck_item_by_attendee_with_ticket(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that an attendee with a ticket can create a potluck item."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        models.Ticket.objects.create(guest_name="Test Guest", event=event, user=nonmember_user, tier=tier)
        url = reverse("api:create_potluck_item", kwargs={"event_id": event.id})
        event.potluck_open = True
        event.save()
        payload = {"name": "Salad", "item_type": "food"}
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200

    def test_create_potluck_item_by_attendee_with_rsvp(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that an attendee with an RSVP can create a potluck item."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        event.potluck_open = True
        event.save()
        url = reverse("api:create_potluck_item", kwargs={"event_id": event.id})
        payload = {"name": "Salad", "item_type": "food"}
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200

    def test_create_potluck_item_by_attendee_without_potluck_open(
        self, nonmember_client: Client, event: Event, nonmember_user: RevelUser
    ) -> None:
        """Test that an attendee with an RSVP can create a potluck item."""
        models.EventRSVP.objects.create(event=event, user=nonmember_user, status=models.EventRSVP.RsvpStatus.YES)
        event.potluck_open = False
        event.save()
        url = reverse("api:create_potluck_item", kwargs={"event_id": event.id})
        payload = {"name": "Salad", "item_type": "food"}
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403
