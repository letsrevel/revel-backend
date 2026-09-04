"""Public ticket-tier listing schema tests."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, EventToken, TicketTier

pytestmark = pytest.mark.django_db


def test_list_tiers_exposes_cancellation_policy_fields(
    client: Client,
    public_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """Buyers must see cancellation/refund policy *before* committing (issue #382)."""
    refund_policy = {
        "tiers": [
            {"hours_before_event": 48, "refund_percentage": "100"},
            {"hours_before_event": 24, "refund_percentage": "50"},
        ],
        "flat_fee": "1.00",
    }
    tier_factory(
        event=public_event,
        name="Cancellable",
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        allow_user_cancellation=True,
        cancellation_deadline_hours=24,
        refund_policy=refund_policy,
    )

    response = client.get(reverse("api:tier_list", kwargs={"event_id": public_event.pk}))

    assert response.status_code == 200, response.content
    body = response.json()
    cancellable = next(t for t in body if t["name"] == "Cancellable")
    assert cancellable["allow_user_cancellation"] is True
    assert cancellable["cancellation_deadline_hours"] == 24
    assert cancellable["refund_policy"] == {
        "tiers": [
            {"hours_before_event": 48, "refund_percentage": "100"},
            {"hours_before_event": 24, "refund_percentage": "50"},
        ],
        "flat_fee": "1.00",
    }


def test_list_tiers_defaults_when_cancellation_disabled(
    client: Client,
    public_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """Tiers without a refund policy serialize defaults — no 500, no missing keys."""
    tier_factory(
        event=public_event,
        name="No-Cancel",
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
    )

    response = client.get(reverse("api:tier_list", kwargs={"event_id": public_event.pk}))

    assert response.status_code == 200, response.content
    body = response.json()
    no_cancel = next(t for t in body if t["name"] == "No-Cancel")
    assert no_cancel["allow_user_cancellation"] is False
    assert no_cancel["cancellation_deadline_hours"] is None
    assert no_cancel["refund_policy"] is None


class TestAnonymousListingWithInvitationLink:
    """An anonymous viewer carrying a granting invitation link sees what the claimed
    invitation would unlock, and ``can_purchase`` says so — guest checkout claims the
    link, so the listing must not steer a link holder away from an invited-only tier.
    """

    @pytest.fixture
    def invited_tier(self, public_event: Event, tier_factory: t.Callable[..., TicketTier]) -> TicketTier:
        return tier_factory(event=public_event, name="Invited Only", purchasable_by=TicketTier.PurchasableBy.INVITED)

    @pytest.fixture
    def private_tier(self, public_event: Event, tier_factory: t.Callable[..., TicketTier]) -> TicketTier:
        return tier_factory(
            event=public_event,
            name="Private",
            visibility=TicketTier.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
        )

    @pytest.fixture
    def link(self, public_event: Event) -> EventToken:
        return EventToken.objects.create(
            event=public_event, issuer=public_event.organization.owner, grants_invitation=True
        )

    @staticmethod
    def _list(client: Client, event: Event, link: EventToken | None = None) -> dict[str, dict[str, t.Any]]:
        headers = {"X-Event-Token": link.pk} if link else None
        response = client.get(reverse("api:tier_list", kwargs={"event_id": event.pk}), headers=headers)
        assert response.status_code == 200, response.content
        return {tier["name"]: tier for tier in response.json()}

    def test_without_link_invited_tier_is_not_purchasable_and_private_tier_hidden(
        self, client: Client, public_event: Event, invited_tier: TicketTier, private_tier: TicketTier
    ) -> None:
        tiers = self._list(client, public_event)

        assert tiers["Invited Only"]["can_purchase"] is False
        assert "Private" not in tiers

    def test_granting_link_unlocks_invited_and_private_tiers(
        self, client: Client, public_event: Event, invited_tier: TicketTier, private_tier: TicketTier, link: EventToken
    ) -> None:
        tiers = self._list(client, public_event, link)

        assert tiers["Invited Only"]["can_purchase"] is True
        assert tiers["Private"]["can_purchase"] is True

    def test_read_only_link_unlocks_nothing(
        self, client: Client, public_event: Event, invited_tier: TicketTier, private_tier: TicketTier, link: EventToken
    ) -> None:
        link.grants_invitation = False
        link.save(update_fields=["grants_invitation"])

        tiers = self._list(client, public_event, link)

        assert tiers["Invited Only"]["can_purchase"] is False
        assert "Private" not in tiers

    def test_purchase_linked_restriction_follows_the_link_tiers(
        self, client: Client, public_event: Event, invited_tier: TicketTier, link: EventToken
    ) -> None:
        invited_tier.restrict_purchase_to_linked_invitations = True
        invited_tier.save(update_fields=["restrict_purchase_to_linked_invitations"])

        assert self._list(client, public_event, link)["Invited Only"]["can_purchase"] is False

        link.ticket_tiers.set([invited_tier])
        assert self._list(client, public_event, link)["Invited Only"]["can_purchase"] is True
