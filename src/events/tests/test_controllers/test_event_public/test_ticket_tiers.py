"""Public ticket-tier listing schema tests."""

import typing as t
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, EventRSVP, EventToken, MembershipTier, Ticket, TicketTier, WaitlistOffer

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


class _TierRow(t.TypedDict):
    """The slice of ``TicketTierSchema`` the listing tests read."""

    name: str
    can_purchase: bool


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
    def _list(client: Client, event: Event, link: EventToken | None = None) -> dict[str, _TierRow]:
        headers = {"X-Event-Token": link.pk} if link else None
        response = client.get(reverse("api:tier_list", kwargs={"event_id": event.pk}), headers=headers)
        assert response.status_code == 200, response.content
        rows = t.cast(list[_TierRow], response.json())
        return {row["name"]: row for row in rows}

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

    def test_membership_tier_restriction_is_never_met_anonymously(
        self,
        client: Client,
        public_event: Event,
        invited_tier: TicketTier,
        link: EventToken,
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        general = MembershipTier.objects.get(organization=public_event.organization, name="General membership")
        invited_tier.restricted_to_membership_tiers.set([general])
        public_tier = tier_factory(
            event=public_event, name="Public Gated", purchasable_by=TicketTier.PurchasableBy.PUBLIC
        )
        public_tier.restricted_to_membership_tiers.set([general])

        tiers = self._list(client, public_event, link)

        assert tiers["Invited Only"]["can_purchase"] is False
        assert tiers["Public Gated"]["can_purchase"] is False


class TestTotalAvailableClampedToEventCap:
    """``total_available`` never exceeds what the event-wide cap still allows (#1207)."""

    @staticmethod
    def _available(client: Client, event: Event) -> dict[str, int | None]:
        response = client.get(reverse("api:tier_list", kwargs={"event_id": event.pk}))
        assert response.status_code == 200, response.content
        # Events come with a default "General Admission" tier; ignore it.
        return {row["name"]: row["total_available"] for row in response.json() if row["name"] != "General Admission"}

    @pytest.fixture(autouse=True)
    def _cap(self, public_event: Event) -> None:
        public_event.max_attendees = 30
        public_event.save(update_fields=["max_attendees"])

    def test_tier_larger_than_cap_is_clamped(
        self, client: Client, public_event: Event, tier_factory: t.Callable[..., TicketTier]
    ) -> None:
        tier_factory(event=public_event, name="Big", total_quantity=40)

        assert self._available(client, public_event) == {"Big": 30}

    def test_sold_tickets_on_other_tier_reduce_it(
        self,
        client: Client,
        public_event: Event,
        tier_factory: t.Callable[..., TicketTier],
        ticket_factory: t.Callable[..., Ticket],
        revel_user_factory: RevelUserFactory,
    ) -> None:
        other = tier_factory(event=public_event, name="Other", total_quantity=100)
        tier_factory(event=public_event, name="Big", total_quantity=40)
        for _ in range(25):
            ticket_factory(event=public_event, tier=other, user=revel_user_factory())

        assert self._available(client, public_event)["Big"] == 5

    def test_non_ticket_event_counts_tickets_not_rsvps(
        self,
        client: Client,
        public_event: Event,
        tier_factory: t.Callable[..., TicketTier],
        ticket_factory: t.Callable[..., Ticket],
        revel_user_factory: RevelUserFactory,
    ) -> None:
        public_event.requires_ticket = False
        public_event.save(update_fields=["requires_ticket"])
        tier = tier_factory(event=public_event, name="Big", total_quantity=40)
        for _ in range(3):
            ticket_factory(event=public_event, tier=tier, user=revel_user_factory())
        for _ in range(5):
            EventRSVP.objects.create(event=public_event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)

        assert self._available(client, public_event)["Big"] == 27

    def test_unlimited_tier_reports_remaining_cap(
        self, client: Client, public_event: Event, tier_factory: t.Callable[..., TicketTier]
    ) -> None:
        tier_factory(event=public_event, name="Open", total_quantity=None)

        assert self._available(client, public_event) == {"Open": 30}

    def test_smaller_tier_keeps_its_own_figure(
        self, client: Client, public_event: Event, tier_factory: t.Callable[..., TicketTier]
    ) -> None:
        tier_factory(event=public_event, name="Small", total_quantity=10)

        assert self._available(client, public_event) == {"Small": 10}

    def test_uncapped_event_returns_raw_tier_value(
        self, client: Client, public_event: Event, tier_factory: t.Callable[..., TicketTier]
    ) -> None:
        public_event.max_attendees = 0
        public_event.save(update_fields=["max_attendees"])
        tier_factory(event=public_event, name="Big", total_quantity=40)
        tier_factory(event=public_event, name="Open", total_quantity=None)

        assert self._available(client, public_event) == {"Big": 40, "Open": None}

    def test_cancelled_tickets_do_not_consume_capacity(
        self,
        client: Client,
        public_event: Event,
        tier_factory: t.Callable[..., TicketTier],
        ticket_factory: t.Callable[..., Ticket],
        revel_user_factory: RevelUserFactory,
    ) -> None:
        tier = tier_factory(event=public_event, name="Big", total_quantity=40)
        ticket_factory(event=public_event, tier=tier, user=revel_user_factory(), status=Ticket.TicketStatus.CANCELLED)
        ticket_factory(event=public_event, tier=tier, user=revel_user_factory())

        assert self._available(client, public_event)["Big"] == 29

    def test_pending_offers_consume_capacity_unless_expired_or_cutoff(
        self,
        client: Client,
        public_event: Event,
        tier_factory: t.Callable[..., TicketTier],
        revel_user_factory: RevelUserFactory,
    ) -> None:
        tier_factory(event=public_event, name="Big", total_quantity=40)
        in_an_hour = timezone.now() + timedelta(hours=1)
        for expires_at, is_cutoff in [
            (in_an_hour, False),
            (in_an_hour, True),
            (timezone.now() - timedelta(hours=1), False),
        ]:
            WaitlistOffer.objects.create(
                event=public_event,
                user=revel_user_factory(),
                expires_at=expires_at,
                batch_id=uuid.uuid4(),
                is_cutoff_batch=is_cutoff,
            )

        assert self._available(client, public_event)["Big"] == 29

    def test_hidden_capacity_stays_hidden(
        self, client: Client, public_event: Event, tier_factory: t.Callable[..., TicketTier]
    ) -> None:
        public_event.visibility_settings = {"show_capacity": False}
        public_event.save(update_fields=["visibility_settings"])
        tier_factory(event=public_event, name="Big", total_quantity=40)

        assert self._available(client, public_event) == {"Big": None}

    def test_query_count_does_not_grow_with_tiers(
        self,
        client: Client,
        public_event: Event,
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        url = reverse("api:tier_list", kwargs={"event_id": public_event.pk})
        tier_factory(event=public_event, name="T1", total_quantity=40)
        with CaptureQueriesContext(connection) as one_tier:
            client.get(url)
        tier_factory(event=public_event, name="T2", total_quantity=40)
        tier_factory(event=public_event, name="T3", total_quantity=None)
        with CaptureQueriesContext(connection) as three_tiers:
            client.get(url)

        assert len(three_tiers) == len(one_tier)
