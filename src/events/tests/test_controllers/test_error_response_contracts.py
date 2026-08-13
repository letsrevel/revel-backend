"""Wire-contract tests for 400 response bodies (#712).

Endpoints declare a 400 schema in OpenAPI, but 400 bodies are produced by three
independent mechanisms that never pass through Ninja's response serialization:

- ``{"detail": ...}`` — a raised ``ninja.errors.HttpError``, or an exception
  mapped by an app's ``exception_handlers.py`` via ``make_simple_handler`` /
  ``make_static_handler``.
- ``{"errors": {field: [msgs]}}`` — the global Django ``ValidationError``
  handler in ``api/exception_handlers.py``.
- the eligibility payload — ``UserIsIneligibleError`` in
  ``events/exception_handlers.py``.

Because none of those are validated against the declaration, the declared schema
and the real body drifted apart: 26 endpoints declared ``ResponseMessage``
(``{"message": ...}``) while no reachable 400 ever produced a ``message`` key.
These tests pin the *actual* wire shape so the declarations stay honest.
"""

import datetime as dt
import json
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.http import HttpRequest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.exception_handlers import HANDLERS
from events.exceptions import (
    EventRefundsStartedError,
    NothingToRefundError,
    RefundInsufficientBalanceError,
    StripeRefundFailed,
)
from events.models import (
    Event,
    EventRSVP,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


def assert_detail_body(response: t.Any) -> str:
    """Assert a 400 whose body is exactly the ``{"detail": ...}`` shape."""
    return assert_detail_body_with_status(response, 400)


def assert_detail_body_with_status(response: t.Any, status_code: int) -> str:
    """Assert ``status_code`` with a body that is exactly the ``{"detail": ...}`` shape."""
    assert response.status_code == status_code, response.content
    body = response.json()
    assert isinstance(body.get("detail"), str) and body["detail"], body
    # The whole point of #712: these endpoints never emit a ``message`` key,
    # so a client typed against ``ResponseMessage`` would read ``undefined``.
    assert "message" not in body, body
    assert "errors" not in body, body
    return t.cast(str, body["detail"])


def assert_eligibility_body(response: t.Any) -> dict[str, t.Any]:
    """Assert a 400 whose body is the serialized ``EventUserEligibility`` payload."""
    assert response.status_code == 400, response.content
    body = response.json()
    assert "message" not in body, body
    # ``reason_code`` / ``next_step`` are what the frontend branches on.
    assert "reason_code" in body, body
    assert "next_step" in body, body
    return t.cast(dict[str, t.Any], body)


class TestWaitlistErrorContracts:
    """``join_waitlist`` / ``leave_waitlist`` declared ``ResponseMessage``; they never emit it."""

    def test_join_waitlist_closed_returns_detail(self, member_client: Client, public_event: Event) -> None:
        public_event.waitlist_open = False
        public_event.save(update_fields=["waitlist_open"])

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.id})
        assert_detail_body(member_client.post(url))

    def test_join_waitlist_ineligible_returns_eligibility_payload(
        self,
        member_client: Client,
        nonmember_user: RevelUser,
        public_event: Event,
    ) -> None:
        """A gate other than capacity answers with the eligibility payload, not ``{detail}``.

        The event is full (so the user is not simply allowed) *and* past its RSVP
        deadline, so ``next_step`` is not a waitlist step and the view raises
        ``UserIsIneligibleError``.
        """
        public_event.requires_ticket = False
        public_event.waitlist_open = True
        public_event.max_attendees = 1
        public_event.rsvp_before = timezone.now() - dt.timedelta(days=1)
        public_event.save()
        EventRSVP.objects.create(event=public_event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.id})
        assert_eligibility_body(member_client.post(url))

    def test_leave_waitlist_closed_returns_detail(self, member_client: Client, public_event: Event) -> None:
        public_event.waitlist_open = False
        public_event.save(update_fields=["waitlist_open"])

        url = reverse("api:leave_waitlist", kwargs={"event_id": public_event.id})
        assert_detail_body(member_client.delete(url))

    def test_join_waitlist_capacity_conflict_returns_detail_409(
        self, member_client: Client, public_event: Event
    ) -> None:
        """The 409 was declared ``ResponseMessage`` too; it is a raised ``HttpError``."""
        public_event.waitlist_open = True
        public_event.requires_ticket = False
        public_event.max_attendees = 10  # room to spare -> eligibility allows
        public_event.save()

        url = reverse("api:join_waitlist", kwargs={"event_id": public_event.id})
        response = member_client.post(url)
        assert response.status_code == 409, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str)
        assert "message" not in body, body


class TestRsvpErrorContracts:
    """``rsvp_event`` declared only ``EventUserEligibility``; ``{detail}`` is co-reachable."""

    def test_rsvp_on_ticketed_event_returns_eligibility_payload(
        self, member_client: Client, public_event: Event
    ) -> None:
        public_event.requires_ticket = True
        public_event.save(update_fields=["requires_ticket"])

        url = reverse("api:rsvp_event", kwargs={"event_id": public_event.id, "answer": "yes"})
        assert_eligibility_body(member_client.post(url, content_type="application/json"))

    def test_rsvp_note_when_notes_disabled_returns_detail(self, member_client: Client, public_event: Event) -> None:
        """The #711 note rejection is a plain ``HttpError(400)`` — ``{detail}``, not eligibility."""
        public_event.requires_ticket = False
        public_event.accept_rsvp_notes = False
        public_event.save(update_fields=["requires_ticket", "accept_rsvp_notes"])

        url = reverse("api:rsvp_event", kwargs={"event_id": public_event.id, "answer": "yes"})
        assert_detail_body(member_client.post(url, data={"note": "hi"}, content_type="application/json"))


class TestTicketCheckoutErrorContracts:
    """``ticket_checkout`` / ``ticket_pwyc_checkout`` declared only ``EventUserEligibility``."""

    @pytest.fixture
    def pwyc_tier(self, public_event: Event) -> TicketTier:
        return TicketTier.objects.create(
            event=public_event,
            name="PWYC",
            price_type=TicketTier.PriceType.PWYC,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
        )

    def test_pwyc_tier_on_fixed_price_endpoint_returns_detail(
        self, member_client: Client, public_event: Event, pwyc_tier: TicketTier
    ) -> None:
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.id, "tier_id": pwyc_tier.id})
        response = member_client.post(url, data={"tickets": [{"guest_name": "A"}]}, content_type="application/json")
        assert_detail_body(response)

    def test_fixed_price_tier_on_pwyc_endpoint_returns_detail(
        self, member_client: Client, public_event: Event, vip_tier: TicketTier
    ) -> None:
        url = reverse("api:ticket_pwyc_checkout", kwargs={"event_id": public_event.id, "tier_id": vip_tier.id})
        response = member_client.post(
            url,
            data={"tickets": [{"guest_name": "A"}], "price_per_ticket": "10.00"},
            content_type="application/json",
        )
        assert_detail_body(response)

    def test_pwyc_below_minimum_returns_detail(
        self, member_client: Client, public_event: Event, pwyc_tier: TicketTier
    ) -> None:
        url = reverse("api:ticket_pwyc_checkout", kwargs={"event_id": public_event.id, "tier_id": pwyc_tier.id})
        response = member_client.post(
            url,
            data={"tickets": [{"guest_name": "A"}], "price_per_ticket": "1.00"},
            content_type="application/json",
        )
        assert_detail_body(response)


class TestMultiTierCheckoutErrorContracts:
    """``multi_tier_checkout`` (#846) declared only ``EventUserEligibility``; the tier-lookup
    404 and the cart-shape/discount 400s all emit ``{detail}``.
    """

    def _url(self, event: Event) -> str:
        from django.urls import reverse

        return reverse("api:multi_tier_checkout", kwargs={"event_id": event.id})

    def test_unknown_tier_returns_404_detail(self, member_client: Client, public_event: Event) -> None:
        import uuid

        response = member_client.post(
            self._url(public_event),
            data={"items": [{"tier_id": str(uuid.uuid4()), "tickets": [{"guest_name": "A"}]}]},
            content_type="application/json",
        )
        assert_detail_body_with_status(response, 404)

    def test_duplicate_tier_returns_detail(
        self, member_client: Client, public_event: Event, vip_tier: TicketTier
    ) -> None:
        response = member_client.post(
            self._url(public_event),
            data={
                "items": [
                    {"tier_id": str(vip_tier.id), "tickets": [{"guest_name": "A"}]},
                    {"tier_id": str(vip_tier.id), "tickets": [{"guest_name": "B"}]},
                ]
            },
            content_type="application/json",
        )
        assert_detail_body(response)

    def test_discount_code_matching_no_tier_returns_detail(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        tier_a = TicketTier.objects.create(
            event=public_event, name="Free A", payment_method=TicketTier.PaymentMethod.FREE
        )
        tier_b = TicketTier.objects.create(
            event=public_event, name="Free B", payment_method=TicketTier.PaymentMethod.FREE
        )
        from events.models.discount_code import DiscountCode

        DiscountCode.objects.create(
            code="CONTRACT10",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
        )
        response = member_client.post(
            self._url(public_event),
            data={
                "items": [
                    {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A"}]},
                    {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B"}]},
                ],
                "discount_code": "CONTRACT10",
            },
            content_type="application/json",
        )
        assert_detail_body(response)


class TestGuestErrorContracts:
    """The three guest endpoints declared ``ResponseMessage``; they emit ``{detail}``/eligibility."""

    def test_guest_rsvp_requires_login_returns_detail(self, client: Client, public_event: Event) -> None:
        public_event.requires_ticket = False
        public_event.can_attend_without_login = False
        public_event.save(update_fields=["requires_ticket", "can_attend_without_login"])

        url = reverse("api:guest_rsvp", kwargs={"event_id": public_event.id, "answer": "yes"})
        response = client.post(
            url,
            data={"email": "guest@example.com", "first_name": "G", "last_name": "U"},
            content_type="application/json",
        )
        assert_detail_body(response)

    def test_guest_ticket_checkout_requires_login_returns_detail(
        self, client: Client, public_event: Event, vip_tier: TicketTier
    ) -> None:
        public_event.can_attend_without_login = False
        public_event.save(update_fields=["can_attend_without_login"])

        url = reverse("api:guest_ticket_checkout", kwargs={"event_id": public_event.id, "tier_id": vip_tier.id})
        response = client.post(
            url,
            data={
                "email": "guest@example.com",
                "first_name": "G",
                "last_name": "U",
                "tickets": [{"guest_name": "G U"}],
            },
            content_type="application/json",
        )
        assert_detail_body(response)

    def test_guest_pwyc_checkout_on_fixed_price_tier_returns_detail(
        self, client: Client, public_event: Event, vip_tier: TicketTier
    ) -> None:
        public_event.can_attend_without_login = True
        public_event.save(update_fields=["can_attend_without_login"])

        url = reverse("api:guest_ticket_pwyc_checkout", kwargs={"event_id": public_event.id, "tier_id": vip_tier.id})
        response = client.post(
            url,
            data={
                "email": "guest@example.com",
                "first_name": "G",
                "last_name": "U",
                "tickets": [{"guest_name": "G U"}],
                "price_per_ticket": "10.00",
            },
            content_type="application/json",
        )
        assert_detail_body(response)

    def test_confirm_guest_action_invalid_token_returns_detail(self, client: Client) -> None:
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": "not-a-jwt"}, content_type="application/json")
        assert_detail_body(response)


class TestCheckoutManagementErrorContracts:
    """``cancel_checkout`` declared ``ResponseMessage``; it emits ``{detail}``."""

    def test_cancel_non_pending_checkout_returns_detail(
        self,
        member_client: Client,
        member_user: RevelUser,
        public_event: Event,
        vip_tier: TicketTier,
    ) -> None:
        ticket = Ticket.objects.create(event=public_event, user=member_user, tier=vip_tier, guest_name="Member User")
        payment = Payment.objects.create(
            ticket=ticket,
            user=member_user,
            stripe_session_id="cs_test_already_paid",
            amount=Decimal("10.00"),
            platform_fee=Decimal("0.50"),
            currency="EUR",
            status=Payment.PaymentStatus.SUCCEEDED,
        )

        url = reverse("api:cancel_checkout", kwargs={"payment_id": payment.id})
        assert_detail_body(member_client.delete(url))


class TestSubscriptionErrorContracts:
    """The member-facing subscription endpoints declared ``ResponseMessage``; they emit ``{detail}``."""

    def test_subscribe_to_offline_plan_returns_eligibility_body(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        """The gate stack answers first: an offline plan 400s with the serialized eligibility verdict.

        Phase 2 (#831) changed this endpoint's 400 contract from plain
        ``{detail}`` to ``MembershipEligibilitySchema | ErrorDetail`` — the
        offline-plan refusal now comes from ``PaymentReadyGate`` as the
        eligibility shape, which the declared response union covers. Still no
        ``message`` key (the #712 contract this file pins).
        """
        from events.models import MembershipTier
        from events.service import subscription_service

        tier = MembershipTier.objects.filter(organization=organization).first()
        assert tier is not None
        plan = subscription_service.create_plan(
            tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
        )

        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = member_client.post(url, data={"plan_id": str(plan.id)}, content_type="application/json")
        assert response.status_code == 400, response.content
        body = response.json()
        assert body["allowed"] is False
        assert body["reason_code"] == "plan_not_online"
        assert "message" not in body, body

    def test_no_subscription_returns_detail_404(self, member_client: Client, organization: Organization) -> None:
        """404 was declared ``ResponseMessage`` too; ``get_object_or_404`` emits ``{detail}``."""
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = member_client.get(url)
        assert response.status_code == 404, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str)
        assert "message" not in body, body

    def test_member_without_permission_gets_detail_403(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        """403 was declared ``ResponseMessage``; the permission denial emits ``{detail}``.

        Reachable on every route of the org-admin controller — ``OrganizationPermission``
        defers to the object-level check inside ``get_one()``.
        """
        url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str)
        assert "message" not in body, body


class TestUnmappedDoesNotExistContracts:
    """Two bare ``.get()``/``DoesNotExist`` paths used to surface as 500s."""

    def test_unknown_tier_id_returns_400_detail_not_500(
        self,
        organization_owner_client: Client,
        event: Event,
        public_user: RevelUser,
    ) -> None:
        """A bad ``tier_ids`` entry is addressable input, not an internal invariant breach."""
        import uuid

        url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
        missing = uuid.uuid4()
        response = organization_owner_client.post(
            url,
            data={"emails": [public_user.email], "tier_ids": [str(missing)]},
            content_type="application/json",
        )
        detail = assert_detail_body(response)
        # The FE needs to say *which* ids were wrong.
        assert str(missing) in detail

    def test_token_foreign_tier_returns_404_detail(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """The token endpoints' 404 was undeclared; it is a ``{detail}`` naming the tier.

        Unlike the other two sites in this class this was never a 500 — both token
        controllers already caught it — but the status was absent from the
        declared response set, so the generated client had no type for it.
        """
        other_event = Event.objects.create(
            organization=organization,
            name="Other Event",
            slug="other-event-contract",
            start="2025-12-01T10:00:00Z",
            end="2025-12-01T12:00:00Z",
        )
        foreign = TicketTier.objects.create(event=other_event, name="Foreign", total_quantity=10)

        url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
        response = organization_owner_client.post(
            url,
            data={"name": "T", "ticket_tier_ids": [str(foreign.id)]},
            content_type="application/json",
        )
        assert response.status_code == 404, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str)
        assert str(foreign.id) in body["detail"]
        assert "message" not in body, body

    def test_unknown_submission_id_returns_404_detail_not_500(
        self,
        organization_owner_client: Client,
        organization: Organization,
        questionnaire: t.Any,
    ) -> None:
        """``evaluate_submission`` used a bare ``.get()`` while its sibling used ``get_object_or_404``."""
        import uuid

        from events.models import OrganizationQuestionnaire

        org_questionnaire = OrganizationQuestionnaire.objects.create(
            organization=organization, questionnaire=questionnaire
        )
        url = reverse(
            "api:evaluate_submission",
            kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": uuid.uuid4()},
        )
        response = organization_owner_client.post(url, data={"status": "approved"}, content_type="application/json")
        assert response.status_code == 404, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str)


class TestRevivalAmountBound:
    """``RevivalRequestSchema.amount`` had no lower bound — negatives reached the ledger."""

    def test_negative_revival_amount_is_rejected(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        url = reverse("api:revive_my_membership_subscription", kwargs={"org_id": organization.id})
        response = member_client.post(
            url,
            data={"amount": "-10.00", "currency": "EUR"},
            content_type="application/json",
        )
        # Rejected by schema validation before any lookup or ledger write.
        assert response.status_code == 422, response.content
        body = response.json()
        assert "message" not in body, body
        # ninja's request-validation 422 carries a *list* under ``detail`` — a
        # different shape from ``ErrorDetail``'s ``{detail: str}``. Pinned here
        # because that distinction is systemically under-declared repo-wide.
        assert isinstance(body.get("detail"), list), body


class TestOrganizerRefundExceptionContracts:
    """Organizer-refund exceptions (#865) have no endpoint yet (later tasks wire one).

    Pin the handler mapping directly by invoking the registered handler
    functions, rather than through a live route — the acceptable fallback
    when the mechanism this file otherwise uses (a real endpoint) doesn't
    exist yet. Once an endpoint exists these should gain a same-shape
    endpoint-level sibling per this file's usual convention.
    """

    @staticmethod
    def _invoke(exc: Exception) -> tuple[int, dict[str, t.Any]]:
        handler = HANDLERS[type(exc)]
        response = handler(HttpRequest(), exc)
        return response.status_code, json.loads(response.content)

    def test_refund_insufficient_balance_returns_402_detail(self) -> None:
        status, body = self._invoke(RefundInsufficientBalanceError())
        assert status == 402
        assert isinstance(body.get("detail"), str) and body["detail"]
        assert "message" not in body, body
        assert "errors" not in body, body

    def test_nothing_to_refund_returns_409_detail(self) -> None:
        status, body = self._invoke(NothingToRefundError("already fully refunded"))
        assert status == 409
        assert body["detail"] == "already fully refunded"
        assert "message" not in body, body

    def test_event_refunds_started_returns_409_detail(self) -> None:
        status, body = self._invoke(EventRefundsStartedError())
        assert status == 409
        assert isinstance(body.get("detail"), str) and body["detail"]
        assert "message" not in body, body

    def test_stripe_refund_failed_returns_502_detail(self) -> None:
        status, body = self._invoke(StripeRefundFailed("card_declined"))
        assert status == 502
        assert body["detail"] == "card_declined"
        assert "message" not in body, body


class TestOrganizerRefundEndpointContracts:
    """Endpoint-level siblings of ``TestOrganizerRefundExceptionContracts`` (#865, task 7).

    ``POST /event-admin/{event_id}/tickets/{ticket_id}/refund`` now exists, so the
    RefundInsufficientBalanceError / NothingToRefundError / StripeRefundFailed
    contracts can be pinned through a real route rather than by invoking the
    handler directly. ``EventRefundsStartedError`` has no endpoint yet (task 8),
    so it keeps its direct-handler-only test above.
    """

    @pytest.fixture
    def online_paid_ticket(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> Ticket:
        tier = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        ticket = ticket_factory(tier=tier)
        payment_factory(
            ticket=ticket,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_contract_test",
        )
        return ticket

    def test_refund_insufficient_balance_returns_402_detail(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        err = stripe.error.InvalidRequestError(message="insufficient", param=None, code="balance_insufficient")
        with mock.patch("stripe.Refund.create", side_effect=err):
            response = organization_owner_client.post(url, data={}, content_type="application/json")
        assert_detail_body_with_status(response, 402)

    def test_nothing_to_refund_returns_409_detail(
        self,
        organization_owner_client: Client,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("25.00"))
        ticket = ticket_factory(tier=tier)
        payment_factory(ticket=ticket, amount=Decimal("25.00"), stripe_payment_intent_id="")
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")
        assert_detail_body_with_status(response, 409)

    def test_stripe_refund_failed_returns_502_detail(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with mock.patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            response = organization_owner_client.post(url, data={}, content_type="application/json")
        assert_detail_body_with_status(response, 502)


class TestTelegramErrorContracts:
    """``connect_account`` declared ``ResponseMessage``; it emits ``{detail}``."""

    def test_invalid_otp_returns_detail(self, member_client: Client) -> None:
        with mock.patch("django.conf.settings.FEATURE_TELEGRAM", True):
            url = reverse("api:connect_account")
            response = member_client.post(url, data={"otp": "123456789"}, content_type="application/json")
        assert_detail_body(response)
