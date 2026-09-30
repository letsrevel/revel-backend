"""Per-organization daily cap on invitation emails to people without a Revel account (#1035)."""

import typing as t

import orjson
import pytest
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventInvitation, Organization, PendingEventInvitation

pytestmark = pytest.mark.django_db


def _invite(client: Client, event: Event, emails: list[str]) -> t.Any:
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    return client.post(url, data=orjson.dumps({"emails": emails}), content_type="application/json")


def _spent(organization: Organization) -> t.Any:
    return cache.get(f"invite-cap:{organization.pk}:{timezone.now():%Y%m%d}")


def test_under_cap_creates_and_charges(settings: t.Any, organization_owner_client: Client, event: Event) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 2
    response = _invite(organization_owner_client, event, ["a1@example.com", "a2@example.com"])
    assert response.status_code == 200, response.content
    assert PendingEventInvitation.objects.filter(event=event).count() == 2
    assert _spent(event.organization) == 2


def test_over_cap_rejects_everything(
    settings: t.Any, organization_owner_client: Client, event: Event, public_user: RevelUser
) -> None:
    """All-or-nothing: neither model gets a row, and the budget is left untouched."""
    settings.PENDING_INVITATION_DAILY_CAP = 3
    assert _invite(organization_owner_client, event, ["a1@example.com"]).status_code == 200

    response = _invite(
        organization_owner_client, event, [public_user.email, "b1@example.com", "b2@example.com", "b3@example.com"]
    )

    assert response.status_code == 400, response.content
    detail = response.json()["detail"]
    assert "3 people" in detail and "only 2 more" in detail and "limit 3 per day" in detail
    assert "midnight UTC" in detail
    assert PendingEventInvitation.objects.filter(event=event).count() == 1
    assert not EventInvitation.objects.filter(event=event).exists()
    assert _spent(event.organization) == 1


def test_existing_users_do_not_count(
    settings: t.Any, organization_owner_client: Client, event: Event, public_user: RevelUser, member_user: RevelUser
) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 1
    response = _invite(organization_owner_client, event, [public_user.email, member_user.email, "new@example.com"])
    assert response.status_code == 200, response.content
    assert response.json()["created_invitations"] == 2
    assert _spent(event.organization) == 1


def test_already_pending_do_not_count(settings: t.Any, organization_owner_client: Client, event: Event) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 1
    assert _invite(organization_owner_client, event, ["p@example.com"]).status_code == 200
    # Re-inviting updates the existing row and sends no new cold email.
    assert _invite(organization_owner_client, event, ["P@Example.com "]).status_code == 200
    assert _spent(event.organization) == 1


def test_payload_duplicates_count_once(settings: t.Any, organization_owner_client: Client, event: Event) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 1
    response = _invite(organization_owner_client, event, ["dup@example.com", "DUP@example.com", "dup@example.com"])
    assert response.status_code == 200, response.content
    assert PendingEventInvitation.objects.filter(event=event).count() == 1
    assert _spent(event.organization) == 1


def test_delete_and_recreate_still_counts(settings: t.Any, organization_owner_client: Client, event: Event) -> None:
    """Deleting a pending invitation does not refund the budget (resend loophole)."""
    settings.PENDING_INVITATION_DAILY_CAP = 1
    assert _invite(organization_owner_client, event, ["r@example.com"]).status_code == 200
    PendingEventInvitation.objects.filter(event=event).delete()
    assert _invite(organization_owner_client, event, ["r@example.com"]).status_code == 400
    assert not PendingEventInvitation.objects.filter(event=event).exists()


def test_events_of_same_org_share_budget(
    settings: t.Any, organization_owner_client: Client, event: Event, public_event: Event
) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 1
    assert _invite(organization_owner_client, event, ["s1@example.com"]).status_code == 200
    assert _invite(organization_owner_client, public_event, ["s2@example.com"]).status_code == 400


def test_other_org_unaffected(
    settings: t.Any,
    organization_owner_client: Client,
    event: Event,
    django_user_model: type[RevelUser],
) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 1
    assert _invite(organization_owner_client, event, ["o1@example.com"]).status_code == 200

    other_owner = django_user_model.objects.create_user(
        username="other_owner", email="other-owner@example.com", password="pass"
    )
    other_org = Organization.objects.create(name="Other", slug="other", owner=other_owner)
    other_event = Event.objects.create(organization=other_org, name="Other", slug="other", start=timezone.now())
    other_client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(other_owner).access_token}")  # type: ignore[attr-defined]
    assert _invite(other_client, other_event, ["o2@example.com"]).status_code == 200
    assert _spent(other_org) == 1


def test_cap_zero_is_unlimited(settings: t.Any, organization_owner_client: Client, event: Event) -> None:
    settings.PENDING_INVITATION_DAILY_CAP = 0
    response = _invite(organization_owner_client, event, [f"u{i}@example.com" for i in range(5)])
    assert response.status_code == 200, response.content
    assert PendingEventInvitation.objects.filter(event=event).count() == 5
    assert _spent(event.organization) is None


def test_more_than_500_emails_is_422(organization_owner_client: Client, event: Event) -> None:
    response = _invite(organization_owner_client, event, [f"m{i}@example.com" for i in range(501)])
    assert response.status_code == 422
    assert not PendingEventInvitation.objects.filter(event=event).exists()
