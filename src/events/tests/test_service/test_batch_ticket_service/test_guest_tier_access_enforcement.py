"""Tests for guest-checkout enforcement of the tier's ``purchasable_by`` rule.

Production incident (2026-09-04): an organizer set a tier to INVITED on an event
open to guest checkout. The guest cart's non-online branch defers ``create_batch``
— where ``_assert_purchasable_by`` runs — to the emailed confirmation click, so
every guest got a 200 and a confirmation email, and every click on the link then
403'd. The initial checkout request has to answer the rule up front.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import (
    Event,
    EventInvitation,
    EventToken,
    MembershipTier,
    Organization,
    PendingEventInvitation,
    TicketTier,
)
from events.schema import TicketPurchaseItem
from events.schema.checkout import BuyerBillingInfoSchema
from events.service import event_service
from events.service import guest as guest_service
from events.service.batch_ticket_service import CartGroup

pytestmark = pytest.mark.django_db


def _token_headers(event_token: str | None) -> dict[str, str] | None:
    """Request headers carrying an invitation link, if any."""
    return {"X-Event-Token": event_token} if event_token else None


@pytest.fixture
def guest_event(organization: Organization) -> Event:
    """Future-dated public event open to unauthenticated (guest) checkout."""
    return Event.objects.create(
        organization=organization,
        name="Guest Tier Access Event",
        slug="guest-tier-access-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        require_ticket_names=False,
        can_attend_without_login=True,
        max_attendees=100,
    )


@pytest.fixture
def invited_tier(guest_event: Event) -> TicketTier:
    """Offline (non-online) tier restricted to invited buyers — the incident's shape."""
    return TicketTier.objects.create(
        event=guest_event,
        name="Invited Only",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=TicketTier.PurchasableBy.INVITED,
    )


def _checkout(event: Event, tier: TicketTier, email: str, *, event_token: str | None = None) -> t.Any:
    headers = _token_headers(event_token)
    return Client().post(
        reverse("api:guest_multi_tier_checkout", kwargs={"event_id": event.pk}),
        data={
            "email": email,
            "first_name": "Guest",
            "last_name": "Buyer",
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Guest Buyer"}]}],
        },
        content_type="application/json",
        headers=headers,
    )


class TestGuestTierAccessEnforcement:
    """``transaction=True`` because the assertion is about the confirmation email,
    whose dispatch is registered with ``transaction.on_commit`` and so never fires
    under pytest-django's wrapping transaction.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_uninvited_guest_rejected_at_checkout_and_no_email(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        response = _checkout(guest_event, invited_tier, "uninvited@example.com")

        assert response.status_code == 403, response.content
        assert "not allowed to purchase from this tier" in response.json()["detail"]
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_email_invited_guest_still_gets_confirmation(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        # The organizer invited this address before it had an account: the pending
        # invitation is converted to a real one when the guest user row is created,
        # so the rule must be evaluated only after that conversion.
        PendingEventInvitation.objects.create(event=guest_event, email="invited@example.com")

        response = _checkout(guest_event, invited_tier, "invited@example.com")

        assert response.status_code == 200, response.content
        assert response.json()["message"]
        assert EventInvitation.objects.filter(event=guest_event, user__email="invited@example.com").exists()
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_tier_linked_invitation_is_honoured(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        invited_tier.restrict_purchase_to_linked_invitations = True
        invited_tier.save(update_fields=["restrict_purchase_to_linked_invitations"])
        other_tier = TicketTier.objects.create(
            event=guest_event,
            name="Other Invited",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
        )
        pending = PendingEventInvitation.objects.create(event=guest_event, email="linked@example.com")
        pending.tiers.set([other_tier])

        response = _checkout(guest_event, invited_tier, "linked@example.com")

        assert response.status_code == 403, response.content
        assert "not allowed to purchase from this tier" in response.json()["detail"]
        mock_send_email.assert_not_called()


@pytest.fixture
def invitation_link(guest_event: Event) -> EventToken:
    """A shareable invitation link for the event, as an organizer would hand out."""
    return EventToken.objects.create(event=guest_event, issuer=guest_event.organization.owner, grants_invitation=True)


class TestGuestInvitationLinkClaim:
    """A guest carrying an invitation link (``X-Event-Token``) claims it at checkout,
    exactly like a logged-in user does on the join page, so the tier rule and the
    later confirmation click both see a real ``EventInvitation``.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_link_holder_is_invited_and_gets_confirmation(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        response = _checkout(guest_event, invited_tier, "linkholder@example.com", event_token=invitation_link.pk)

        assert response.status_code == 200, response.content
        assert EventInvitation.objects.filter(event=guest_event, user__email="linkholder@example.com").exists()
        invitation_link.refresh_from_db()
        assert invitation_link.uses == 1
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_returning_guest_does_not_consume_a_second_use(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        _checkout(guest_event, invited_tier, "returning@example.com", event_token=invitation_link.pk)
        response = _checkout(guest_event, invited_tier, "returning@example.com", event_token=invitation_link.pk)

        assert response.status_code == 200, response.content
        invitation_link.refresh_from_db()
        assert invitation_link.uses == 1

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_read_only_link_does_not_invite(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        invitation_link.grants_invitation = False
        invitation_link.save(update_fields=["grants_invitation"])

        response = _checkout(guest_event, invited_tier, "readonly@example.com", event_token=invitation_link.pk)

        assert response.status_code == 403, response.content
        assert not EventInvitation.objects.filter(event=guest_event, user__email="readonly@example.com").exists()
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_link_for_another_event_is_ignored(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier, organization: Organization
    ) -> None:
        other_event = Event.objects.create(
            organization=organization,
            name="Other Event",
            slug="other-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )
        foreign_link = EventToken.objects.create(event=other_event, issuer=organization.owner, grants_invitation=True)

        response = _checkout(guest_event, invited_tier, "foreign@example.com", event_token=foreign_link.pk)

        assert response.status_code == 403, response.content
        assert not EventInvitation.objects.filter(user__email="foreign@example.com").exists()
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_tier_linked_link_satisfies_linked_restriction(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        invited_tier.restrict_purchase_to_linked_invitations = True
        invited_tier.save(update_fields=["restrict_purchase_to_linked_invitations"])
        invitation_link.ticket_tiers.set([invited_tier])

        response = _checkout(guest_event, invited_tier, "tierlink@example.com", event_token=invitation_link.pk)

        assert response.status_code == 200, response.content
        mock_send_email.assert_called_once()


class TestGuestInvitationLinkRsvp:
    """Same claim on the guest RSVP path: a private RSVP event's invitation gate
    otherwise blocks every guest, link or no link.
    """

    @pytest.fixture
    def private_rsvp_event(self, organization: Organization) -> Event:
        return Event.objects.create(
            organization=organization,
            name="Private RSVP Event",
            slug="private-rsvp-event",
            event_type=Event.EventType.PRIVATE,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            requires_ticket=False,
            can_attend_without_login=True,
            max_attendees=100,
        )

    @staticmethod
    def _rsvp(event: Event, email: str, *, event_token: str | None = None) -> t.Any:
        headers = _token_headers(event_token)
        return Client().post(
            reverse("api:guest_rsvp", kwargs={"event_id": event.pk, "answer": "yes"}),
            data={"email": email, "first_name": "Guest", "last_name": "Rsvp"},
            content_type="application/json",
            headers=headers,
        )

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_without_link_private_event_blocks_guest(self, mock_send_email: Mock, private_rsvp_event: Event) -> None:
        response = self._rsvp(private_rsvp_event, "nolink@example.com")

        assert response.status_code == 400, response.content
        assert response.json()["allowed"] is False
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_link_holder_can_rsvp(self, mock_send_email: Mock, private_rsvp_event: Event) -> None:
        link = EventToken.objects.create(
            event=private_rsvp_event, issuer=private_rsvp_event.organization.owner, grants_invitation=True
        )

        response = self._rsvp(private_rsvp_event, "rsvplink@example.com", event_token=link.pk)

        assert response.status_code == 200, response.content
        assert EventInvitation.objects.filter(event=private_rsvp_event, user__email="rsvplink@example.com").exists()
        mock_send_email.assert_called_once()


@pytest.fixture
def private_invited_tier(guest_event: Event) -> TicketTier:
    """Invitation-gated tier that is also hidden (PRIVATE visibility): only a link can reach it."""
    return TicketTier.objects.create(
        event=guest_event,
        name="Private Invited",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        visibility=TicketTier.Visibility.PRIVATE,
        purchasable_by=TicketTier.PurchasableBy.INVITED,
    )


class TestGuestInvitationLinkPrivateTier:
    """PRIVATE tiers are invisible to anonymous users, and the guest routes resolve
    the tier BEFORE the claim can run — so a link to a private tier used to 404. The
    tier lookup now grants the visibility the claimed invitation would grant.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_without_link_private_tier_is_not_found(
        self, mock_send_email: Mock, guest_event: Event, private_invited_tier: TicketTier
    ) -> None:
        response = _checkout(guest_event, private_invited_tier, "nolink@example.com")

        assert response.status_code == 404, response.content
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_link_unlocks_private_tier(
        self, mock_send_email: Mock, guest_event: Event, private_invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        response = _checkout(guest_event, private_invited_tier, "private@example.com", event_token=invitation_link.pk)

        assert response.status_code == 200, response.content
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_link_unlocks_private_tier_on_deprecated_single_tier_route(
        self, mock_send_email: Mock, guest_event: Event, private_invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        response = Client().post(
            reverse(
                "api:guest_ticket_checkout", kwargs={"event_id": guest_event.pk, "tier_id": private_invited_tier.pk}
            ),
            data={
                "email": "legacy@example.com",
                "first_name": "Guest",
                "last_name": "Legacy",
                "tickets": [{"guest_name": "Guest Legacy"}],
            },
            content_type="application/json",
            headers=_token_headers(invitation_link.pk),
        )

        assert response.status_code == 200, response.content
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_visibility_linked_restriction_needs_a_linked_link(
        self, mock_send_email: Mock, guest_event: Event, private_invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        private_invited_tier.restrict_visibility_to_linked_invitations = True
        private_invited_tier.save(update_fields=["restrict_visibility_to_linked_invitations"])

        unlinked = _checkout(guest_event, private_invited_tier, "unlinked@example.com", event_token=invitation_link.pk)
        assert unlinked.status_code == 404, unlinked.content

        invitation_link.ticket_tiers.set([private_invited_tier])
        linked = _checkout(guest_event, private_invited_tier, "linked@example.com", event_token=invitation_link.pk)
        assert linked.status_code == 200, linked.content
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_read_only_link_does_not_unlock_private_tier(
        self, mock_send_email: Mock, guest_event: Event, private_invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        invitation_link.grants_invitation = False
        invitation_link.save(update_fields=["grants_invitation"])

        response = _checkout(guest_event, private_invited_tier, "readonly@example.com", event_token=invitation_link.pk)

        assert response.status_code == 404, response.content
        mock_send_email.assert_not_called()


class TestGuestMembershipTierEnforcement:
    """The membership-tier restriction is the sibling of ``purchasable_by`` and was
    equally deferred to the confirmation click on the non-online guest path.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_membership_gated_tier_rejected_at_checkout_and_no_email(
        self, mock_send_email: Mock, guest_event: Event, organization: Organization
    ) -> None:
        gated_tier = TicketTier.objects.create(
            event=guest_event, name="Members Only", payment_method=TicketTier.PaymentMethod.OFFLINE
        )
        gated_tier.restricted_to_membership_tiers.set(
            [MembershipTier.objects.get(organization=organization, name="General membership")]
        )

        response = _checkout(guest_event, gated_tier, "nonmember@example.com")

        assert response.status_code == 400, response.content
        assert response.json()["reason_code"] == "membership_tier_required"
        mock_send_email.assert_not_called()


class TestGuestClaimNeverHoldsTokenLockAcrossVies:
    """Claiming the link locks the EventToken row for the rest of the request, so the
    VIES round-trip must already be done by then (#632 discipline) and handed to
    create_batch rather than resolved again under the lock.
    """

    def test_vies_resolves_before_the_claim_and_is_reused(
        self, guest_event: Event, invitation_link: EventToken
    ) -> None:
        online_tier = TicketTier.objects.create(
            event=guest_event, name="Online", payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("10.00")
        )
        billing_info = BuyerBillingInfoSchema(billing_name="ACME", vat_id="ATU12345678", vat_country_code="AT")
        order = Mock()
        with (
            patch("events.service.stripe_service.resolve_attendee_vat_for_reserve", return_value="vat-ctx") as vies,
            patch("events.service.tokens.claim_invitation") as claim,
            patch(
                "events.service.batch_ticket_service.service.BatchTicketService.create_batch",
                return_value=([], uuid4()),
            ) as create_batch,
        ):
            order.attach_mock(vies, "vies")
            order.attach_mock(claim, "claim")
            guest_service.handle_guest_ticket_checkout(
                guest_event,
                [CartGroup(tier=online_tier, items=[TicketPurchaseItem(guest_name="Guest Vies")])],
                "vies@example.com",
                "Guest",
                "Vies",
                billing_info=billing_info,
                event_token=invitation_link,
            )

        call_names = [name for name, _args, _kwargs in order.mock_calls]
        assert call_names.index("vies") < call_names.index("claim")
        create_batch.assert_called_once()
        assert create_batch.call_args.kwargs["buyer_vat_context"] == "vat-ctx"


class TestEventTokenResolvedOnce:
    """``get_one`` already resolves the token for event visibility; the claim must reuse it."""

    def test_guest_checkout_looks_the_token_up_once(
        self, guest_event: Event, invited_tier: TicketTier, invitation_link: EventToken
    ) -> None:
        with patch(
            "events.controllers.event_public.base.event_service.get_event_token", wraps=event_service.get_event_token
        ) as lookup:
            response = _checkout(guest_event, invited_tier, "once@example.com", event_token=invitation_link.pk)

        assert response.status_code == 200, response.content
        lookup.assert_called_once_with(invitation_link.pk)
