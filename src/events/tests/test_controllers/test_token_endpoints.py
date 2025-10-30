"""Tests for token GET endpoints and validation."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.service import event_service, organization_service

pytestmark = pytest.mark.django_db


# --- Tests for GET /events/tokens/{token_id} ---


def test_get_event_token_returns_token_details(
    client: Client, event: Event, organization_owner_user: RevelUser
) -> None:
    """Test that GET /events/tokens/{token_id} returns token details without authentication."""
    # Arrange
    token = event_service.create_event_token(event=event, issuer=organization_owner_user, name="Test Token")
    url = reverse("api:get_event_token", kwargs={"token_id": token.id})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == token.id
    assert data["name"] == "Test Token"
    assert data["event"] is not None
    assert data["grants_invitation"] is True


def test_get_event_token_shows_ticket_tier_when_present(
    client: Client, event: Event, organization_owner_user: RevelUser, vip_tier: TicketTier
) -> None:
    """Test that GET /events/tokens/{token_id} includes ticket_tier when set."""
    # Arrange
    token = event_service.create_event_token(
        event=event, issuer=organization_owner_user, name="VIP Token", ticket_tier_id=vip_tier.id
    )
    url = reverse("api:get_event_token", kwargs={"token_id": token.id})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["ticket_tier"] is not None
    assert str(data["ticket_tier"]) == str(vip_tier.id)


def test_get_event_token_returns_404_for_invalid_token(client: Client) -> None:
    """Test that GET /events/tokens/{token_id} returns 404 for invalid token."""
    # Arrange
    url = reverse("api:get_event_token", kwargs={"token_id": "invalid-token"})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 404


def test_get_event_token_returns_404_for_expired_token(
    client: Client, event: Event, organization_owner_user: RevelUser
) -> None:
    """Test that GET /events/tokens/{token_id} returns 404 for expired token."""
    # Arrange
    token = event_service.create_event_token(event=event, issuer=organization_owner_user, duration=-60)
    url = reverse("api:get_event_token", kwargs={"token_id": token.id})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 404


# --- Tests for GET /organizations/tokens/{token_id} ---


def test_get_organization_token_returns_token_details(
    client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that GET /organizations/tokens/{token_id} returns token details without authentication."""
    # Arrange
    token = organization_service.create_organization_token(
        organization=organization, issuer=organization_owner_user, name="Member Invite"
    )
    url = reverse("api:get_organization_token", kwargs={"token_id": token.id})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == token.id
    assert data["name"] == "Member Invite"
    assert data["organization"] is not None
    assert data["grants_membership"] is True


def test_get_organization_token_shows_staff_status(
    client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that GET /organizations/tokens/{token_id} shows grants_staff_status."""
    # Arrange
    token = organization_service.create_organization_token(
        organization=organization, issuer=organization_owner_user, name="Staff Invite", grants_staff_status=True
    )
    url = reverse("api:get_organization_token", kwargs={"token_id": token.id})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["grants_staff_status"] is True


def test_get_organization_token_returns_404_for_invalid_token(client: Client) -> None:
    """Test that GET /organizations/tokens/{token_id} returns 404 for invalid token."""
    # Arrange
    url = reverse("api:get_organization_token", kwargs={"token_id": "invalid-token"})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 404


# --- Tests for ticket_tier_id validation ---


def test_create_event_token_requires_ticket_tier_for_ticketed_events(
    organization_owner_client: Client, event: Event, vip_tier: TicketTier
) -> None:
    """Test that ticket_tier_id is required when event.requires_ticket is True."""
    # Arrange - event fixture has requires_ticket=True by default
    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {"name": "Test Token", "max_uses": 10, "duration": 60}

    # Act
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 400
    assert b"ticket_tier_id is required" in response.content


def test_create_event_token_succeeds_with_ticket_tier_for_ticketed_events(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that token creation succeeds when ticket_tier_id is provided for ticketed events."""
    # Arrange - use event_ticket_tier which belongs to event
    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {"name": "Test Token", "max_uses": 10, "duration": 60, "ticket_tier_id": str(event_ticket_tier.id)}

    # Act
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Token"
    assert str(data["ticket_tier"]) == str(event_ticket_tier.id)


def test_create_event_token_allows_null_ticket_tier_for_non_ticketed_events(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that ticket_tier_id can be null when event.requires_ticket is False."""
    # Arrange - create a non-ticketed event
    non_ticketed_event = Event.objects.create(
        organization=organization,
        name="Non-Ticketed Event",
        slug="non-ticketed-event",
        requires_ticket=False,
        start="2025-12-01T10:00:00Z",
        end="2025-12-01T12:00:00Z",
    )
    url = reverse("api:create_event_token", kwargs={"event_id": non_ticketed_event.pk})
    payload = {"name": "Test Token", "max_uses": 10, "duration": 60}

    # Act
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Token"
    assert data["ticket_tier"] is None


def test_create_event_token_validates_ticket_tier_belongs_to_event(
    organization_owner_client: Client, event: Event, organization_owner_user: RevelUser, organization: Organization
) -> None:
    """Test that ticket_tier_id must belong to the event."""
    # Arrange - create a different event with its own tier
    other_event = Event.objects.create(
        organization=organization,
        name="Other Event",
        slug="other-event",
        start="2025-12-01T10:00:00Z",
        end="2025-12-01T12:00:00Z",
    )
    other_tier = TicketTier.objects.create(
        event=other_event, name="Other Tier", price=50, total_quantity=100, payment_method="online"
    )

    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {"name": "Test Token", "ticket_tier_id": str(other_tier.id)}

    # Act
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 404  # tier not found for this event


# --- Tests for token visibility use case ---


def test_event_token_grants_visibility_via_header(client: Client, private_event: Event, public_user: RevelUser) -> None:
    """Test that X-Event-Token header grants visibility to private events."""
    # Arrange - create a read-only token (no invitation)
    token = event_service.create_event_token(event=private_event, issuer=private_event.organization.owner)
    token.grants_invitation = False
    token.save()

    # Act - access event with token header (without authentication)
    url = reverse("api:get_event", kwargs={"event_id": private_event.pk})
    response = client.get(url, HTTP_X_EVENT_TOKEN=token.id)

    # Assert - should be able to see the event
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(private_event.pk)


def test_organization_token_grants_visibility_via_header(client: Client, organization_owner_user: RevelUser) -> None:
    """Test that X-Organization-Token header grants visibility to private organizations."""
    # Arrange - create a private organization
    private_org = Organization.objects.create(
        name="Private Org", slug="private-org", owner=organization_owner_user, visibility="private"
    )
    # Create a token (grants_membership=True by default which is fine for visibility)
    token = organization_service.create_organization_token(organization=private_org, issuer=organization_owner_user)

    # Act - access organization with token header (without authentication)
    url = reverse("api:get_organization", kwargs={"slug": private_org.slug})
    response = client.get(url, HTTP_X_ORG_TOKEN=token.id)

    # Assert - should be able to see the organization
    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == private_org.slug


# --- Tests for backwards compatibility with query params ---


def test_event_token_backwards_compatible_with_query_param(
    client: Client, private_event: Event, public_user: RevelUser
) -> None:
    """Test that ?et= query param still works for backwards compatibility."""
    # Arrange - create a read-only token (no invitation)
    token = event_service.create_event_token(event=private_event, issuer=private_event.organization.owner)
    token.grants_invitation = False
    token.save()

    # Act - access event with legacy query param
    url = reverse("api:get_event", kwargs={"event_id": private_event.pk})
    response = client.get(f"{url}?et={token.id}")

    # Assert - should still work
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(private_event.pk)


def test_organization_token_backwards_compatible_with_query_param(
    client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that ?ot= query param still works for backwards compatibility."""
    # Arrange - create a private organization
    private_org = Organization.objects.create(
        name="Private Org Legacy", slug="private-org-legacy", owner=organization_owner_user, visibility="private"
    )
    token = organization_service.create_organization_token(organization=private_org, issuer=organization_owner_user)

    # Act - access organization with legacy query param
    url = reverse("api:get_organization", kwargs={"slug": private_org.slug})
    response = client.get(f"{url}?ot={token.id}")

    # Assert - should still work
    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == private_org.slug
