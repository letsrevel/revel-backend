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
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
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
    assert response.status_code == 400, response.content
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

    def test_subscribe_to_offline_plan_returns_detail(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        from events.models import MembershipTier
        from events.service import subscription_service

        tier = MembershipTier.objects.filter(organization=organization).first()
        assert tier is not None
        plan = subscription_service.create_plan(
            tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
        )

        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = member_client.post(url, data={"plan_id": str(plan.id)}, content_type="application/json")
        assert_detail_body(response)

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


class TestTelegramErrorContracts:
    """``connect_account`` declared ``ResponseMessage``; it emits ``{detail}``."""

    def test_invalid_otp_returns_detail(self, member_client: Client) -> None:
        with mock.patch("django.conf.settings.FEATURE_TELEGRAM", True):
            url = reverse("api:connect_account")
            response = member_client.post(url, data={"otp": "123456789"}, content_type="application/json")
        assert_detail_body(response)
