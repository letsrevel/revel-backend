"""Tests for waitlist endpoints."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventWaitList

pytestmark = pytest.mark.django_db


# ===== Tests for POST /events/{event_id}/waitlist/join =====


class TestJoinWaitlist:
    """Test joining the event waitlist."""

    def test_join_waitlist_success(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
    ) -> None:
        """Test successfully joining an event waitlist.

        When a user joins a waitlist for an event with an open waitlist,
        they should be added to the waitlist and receive a success message.
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.post(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Successfully joined the waitlist."

        # Verify database state
        assert EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_join_waitlist_already_on_waitlist(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
    ) -> None:
        """Test joining waitlist when already on it.

        When a user attempts to join a waitlist they're already on,
        they should receive a message indicating they're already on the waitlist
        without creating a duplicate entry.
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.post(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "You are already on the waitlist for this event."

        # Verify no duplicate created
        assert EventWaitList.objects.filter(event=public_event, user=member_user).count() == 1

    def test_join_waitlist_not_open(
        self,
        member_client: Client,
        public_event: Event,
    ) -> None:
        """Test joining waitlist when it's not open.

        When attempting to join a waitlist that is not open,
        the request should be rejected with a 400 error.
        """
        # Arrange
        public_event.waitlist_open = False
        public_event.save()

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.post(url)

        # Assert
        assert response.status_code == 400
        # HttpError in Django Ninja returns the message directly as a string
        data = response.json()
        assert "This event does not have an open waitlist" in str(data)

    def test_join_waitlist_unauthenticated(
        self,
        client: Client,
        public_event: Event,
    ) -> None:
        """Test that unauthenticated users cannot join waitlist.

        When an unauthenticated user attempts to join a waitlist,
        the request should be rejected with a 401 error.
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = client.post(url)

        # Assert
        assert response.status_code == 401

    def test_join_waitlist_event_not_found(
        self,
        member_client: Client,
    ) -> None:
        """Test joining waitlist for non-existent event.

        When attempting to join a waitlist for a non-existent event,
        the request should be rejected with a 404 error.
        """
        # Arrange
        import uuid

        fake_uuid = uuid.uuid4()
        url = reverse("api:join_waitlist", kwargs={"event_id": fake_uuid})

        # Act
        response = member_client.post(url)

        # Assert
        assert response.status_code == 404


# ===== Tests for DELETE /events/{event_id}/waitlist/leave =====


class TestLeaveWaitlist:
    """Test leaving the event waitlist."""

    def test_leave_waitlist_success(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
    ) -> None:
        """Test successfully leaving an event waitlist.

        When a user leaves a waitlist for an event with an open waitlist,
        they should be removed from the waitlist and receive a success message.
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse("api:leave_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.delete(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Successfully left the waitlist."

        # Verify database state
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_leave_waitlist_not_on_waitlist(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
    ) -> None:
        """Test leaving waitlist when not on it.

        When a user attempts to leave a waitlist they're not on,
        they should receive a success message (idempotent operation).
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()

        url = reverse("api:leave_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.delete(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Successfully left the waitlist."

        # Verify no entry exists
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_leave_waitlist_not_open(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
    ) -> None:
        """Test leaving waitlist when it's not open.

        When attempting to leave a waitlist that is not open,
        the request should be rejected with a 400 error.
        """
        # Arrange
        public_event.waitlist_open = False
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse("api:leave_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.delete(url)

        # Assert
        assert response.status_code == 400
        # HttpError in Django Ninja returns the message directly as a string
        data = response.json()
        assert "This event does not have an open waitlist" in str(data)

        # Verify user is still on waitlist
        assert EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_leave_waitlist_unauthenticated(
        self,
        client: Client,
        public_event: Event,
    ) -> None:
        """Test that unauthenticated users cannot leave waitlist.

        When an unauthenticated user attempts to leave a waitlist,
        the request should be rejected with a 401 error.
        """
        # Arrange
        public_event.waitlist_open = True
        public_event.save()

        url = reverse("api:leave_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = client.delete(url)

        # Assert
        assert response.status_code == 401

    def test_leave_waitlist_event_not_found(
        self,
        member_client: Client,
    ) -> None:
        """Test leaving waitlist for non-existent event.

        When attempting to leave a waitlist for a non-existent event,
        the request should be rejected with a 404 error.
        """
        # Arrange
        import uuid

        fake_uuid = uuid.uuid4()
        url = reverse("api:leave_waitlist", kwargs={"event_id": fake_uuid})

        # Act
        response = member_client.delete(url)

        # Assert
        assert response.status_code == 404


# ===== Tests for GET /event-admin/{event_id}/waitlist =====


class TestListWaitlist:
    """Test listing waitlist entries."""

    def test_list_waitlist_as_owner(
        self,
        organization_owner_client: Client,
        public_event: Event,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test event owner can list all waitlist entries.

        Event owners should be able to see all users on the waitlist,
        ordered by join time (FIFO), with user details included.
        """
        # Arrange
        entry1 = EventWaitList.objects.create(event=public_event, user=member_user)
        entry2 = EventWaitList.objects.create(event=public_event, user=nonmember_user)

        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()

        assert data["count"] == 2
        assert len(data["results"]) == 2

        # Verify ordering (FIFO - oldest first)
        assert data["results"][0]["id"] == str(entry1.pk)
        assert data["results"][0]["user"]["email"] == member_user.email
        assert data["results"][1]["id"] == str(entry2.pk)
        assert data["results"][1]["user"]["email"] == nonmember_user.email

    def test_list_waitlist_as_staff_with_permission(
        self,
        organization_staff_client: Client,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test staff with invite_to_event permission can list waitlist.

        Staff members with the invite_to_event permission should be able
        to view the waitlist entries.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = organization_staff_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_waitlist_unauthorized(
        self,
        member_client: Client,
        public_event: Event,
    ) -> None:
        """Test that regular members cannot list waitlist.

        Regular members without admin permissions should receive
        a 403 error when trying to view the waitlist.
        """
        # Arrange
        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 403

    def test_list_waitlist_empty(
        self,
        organization_owner_client: Client,
        public_event: Event,
    ) -> None:
        """Test listing waitlist when it's empty.

        When there are no users on the waitlist, the endpoint should
        return an empty list with count 0.
        """
        # Arrange
        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert len(data["results"]) == 0

    def test_list_waitlist_pagination(
        self,
        organization_owner_client: Client,
        public_event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test waitlist pagination works correctly.

        The waitlist endpoint should support pagination with a page size of 20.
        """
        # Arrange - Create 25 waitlist entries
        for _ in range(25):
            user = revel_user_factory()
            EventWaitList.objects.create(event=public_event, user=user)

        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act - Get first page
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 25
        assert len(data["results"]) == 20  # Default page size

        # Act - Get second page
        response_page2 = organization_owner_client.get(url, {"page": 2})

        # Assert
        assert response_page2.status_code == 200
        data_page2 = response_page2.json()
        assert len(data_page2["results"]) == 5  # Remaining entries

    def test_list_waitlist_search_by_email(
        self,
        organization_owner_client: Client,
        public_event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test searching waitlist by user email.

        The waitlist endpoint should support searching by user email.
        """
        # Arrange
        EventWaitList.objects.all().delete()
        user1 = revel_user_factory(email="john@example.com")
        user2 = revel_user_factory(email="jane@example.com")
        EventWaitList.objects.create(event=public_event, user=user1)
        EventWaitList.objects.create(event=public_event, user=user2)

        url = reverse("api:list_waitlist", kwargs={"event_id": public_event.pk})

        # Act
        response = organization_owner_client.get(url, {"search": "john"})

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == "john@example.com"


# ===== Tests for DELETE /event-admin/{event_id}/waitlist/{waitlist_id} =====


class TestDeleteWaitlistEntry:
    """Test removing users from the waitlist."""

    def test_delete_waitlist_entry_as_owner(
        self,
        organization_owner_client: Client,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test event owner can remove users from waitlist.

        Event owners should be able to manually remove users from the waitlist.
        The entry should be deleted from the database.
        """
        # Arrange
        entry = EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse(
            "api:delete_waitlist_entry",
            kwargs={"event_id": public_event.pk, "waitlist_id": entry.pk},
        )

        # Act
        response = organization_owner_client.delete(url)

        # Assert
        assert response.status_code == 204

        # Verify database state
        assert not EventWaitList.objects.filter(pk=entry.pk).exists()

    def test_delete_waitlist_entry_as_staff_with_permission(
        self,
        organization_staff_client: Client,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test staff with invite_to_event permission can remove from waitlist.

        Staff members with the invite_to_event permission should be able
        to remove users from the waitlist.
        """
        # Arrange
        entry = EventWaitList.objects.create(event=public_event, user=member_user)

        url = reverse(
            "api:delete_waitlist_entry",
            kwargs={"event_id": public_event.pk, "waitlist_id": entry.pk},
        )

        # Act
        response = organization_staff_client.delete(url)

        # Assert
        assert response.status_code == 204
        assert not EventWaitList.objects.filter(pk=entry.pk).exists()

    def test_delete_waitlist_entry_unauthorized(
        self,
        member_client: Client,
        public_event: Event,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that regular members cannot remove from waitlist.

        Regular members without admin permissions should receive
        a 403 error when trying to delete waitlist entries.
        """
        # Arrange
        entry = EventWaitList.objects.create(event=public_event, user=nonmember_user)

        url = reverse(
            "api:delete_waitlist_entry",
            kwargs={"event_id": public_event.pk, "waitlist_id": entry.pk},
        )

        # Act
        response = member_client.delete(url)

        # Assert
        assert response.status_code == 403

        # Verify entry still exists
        assert EventWaitList.objects.filter(pk=entry.pk).exists()

    def test_delete_waitlist_entry_not_found(
        self,
        organization_owner_client: Client,
        public_event: Event,
    ) -> None:
        """Test deleting non-existent waitlist entry.

        When attempting to delete a waitlist entry that doesn't exist,
        the request should be rejected with a 404 error.
        """
        # Arrange
        import uuid

        fake_uuid = uuid.uuid4()
        url = reverse(
            "api:delete_waitlist_entry",
            kwargs={"event_id": public_event.pk, "waitlist_id": fake_uuid},
        )

        # Act
        response = organization_owner_client.delete(url)

        # Assert
        assert response.status_code == 404

    def test_delete_waitlist_entry_wrong_event(
        self,
        organization_owner_client: Client,
        public_event: Event,
        members_only_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test deleting waitlist entry from wrong event.

        When attempting to delete a waitlist entry using a different event ID,
        the request should be rejected with a 404 error.
        """
        # Arrange - Create entry for public_event
        entry = EventWaitList.objects.create(event=public_event, user=member_user)

        # Try to delete using members_only_event URL
        url = reverse(
            "api:delete_waitlist_entry",
            kwargs={"event_id": members_only_event.pk, "waitlist_id": entry.pk},
        )

        # Act
        response = organization_owner_client.delete(url)

        # Assert
        assert response.status_code == 404

        # Verify entry still exists
        assert EventWaitList.objects.filter(pk=entry.pk).exists()
