"""Every checkout entry point accepts ``attribution`` and stamps it on the tickets (#922).

Authenticated routes write tickets in the request. Guest routes on non-online tiers
mint a confirmation JWT instead, so the tags have to survive the round trip through
the token; guest ONLINE carts reserve PENDING tickets immediately.
"""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Organization, Ticket, TicketTier

pytestmark = pytest.mark.django_db

ATTRIBUTION = {"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": "spring-2026"}


def _tier(event: Event, name: str, **kwargs: object) -> TicketTier:
    defaults: dict[str, object] = {
        "price": Decimal("20.00"),
        "currency": "EUR",
        "payment_method": TicketTier.PaymentMethod.OFFLINE,
        "total_quantity": 100,
    }
    defaults.update(kwargs)
    return TicketTier.objects.create(event=event, name=name, **defaults)


def _attributions(event: Event) -> list[dict[str, str] | None]:
    return [ticket.attribution for ticket in Ticket.objects.filter(event=event)]


# ---- Authenticated routes ----


@pytest.fixture
def public_event(public_event: Event) -> Event:
    """The shared public event, with room for a three-ticket cart."""
    public_event.max_tickets_per_user = 5
    public_event.save(update_fields=["max_tickets_per_user"])
    return public_event


def test_multi_tier_checkout_stamps_attribution(member_client: Client, public_event: Event) -> None:
    tier_a, tier_b = _tier(public_event, "A"), _tier(public_event, "B")
    payload = {
        "items": [
            {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "Ann"}]},
            {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "Bob"}, {"guest_name": "Cy"}]},
        ],
        "attribution": ATTRIBUTION,
    }
    url = reverse("api:multi_tier_checkout", kwargs={"event_id": public_event.pk})
    response = member_client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert _attributions(public_event) == [ATTRIBUTION] * 3


def test_single_tier_checkout_stamps_attribution(member_client: Client, public_event: Event) -> None:
    tier = _tier(public_event, "A")
    payload = {"tickets": [{"guest_name": "Ann"}], "attribution": ATTRIBUTION}
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": tier.pk})
    response = member_client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert _attributions(public_event) == [ATTRIBUTION]


def test_pwyc_checkout_stamps_attribution(member_client: Client, public_event: Event) -> None:
    tier = _tier(
        public_event,
        "PWYC",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5.00"),
        pwyc_max=Decimal("50.00"),
    )
    payload = {"tickets": [{"guest_name": "Ann"}], "price_per_ticket": "10.00", "attribution": ATTRIBUTION}
    url = reverse("api:ticket_pwyc_checkout", kwargs={"event_id": public_event.pk, "tier_id": tier.pk})
    response = member_client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert _attributions(public_event) == [ATTRIBUTION]


def test_attribution_is_sanitised_at_the_boundary(member_client: Client, public_event: Event) -> None:
    tier = _tier(public_event, "A")
    payload = {
        "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}],
        "attribution": {"utm_source": "ok", "utm_term": "dropped", "utm_medium": "has space"},
    }
    url = reverse("api:multi_tier_checkout", kwargs={"event_id": public_event.pk})
    response = member_client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert _attributions(public_event) == [{"utm_source": "ok"}]


def test_omitted_attribution_is_null(member_client: Client, public_event: Event) -> None:
    tier = _tier(public_event, "A")
    payload = {"items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}]}
    url = reverse("api:multi_tier_checkout", kwargs={"event_id": public_event.pk})
    response = member_client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert _attributions(public_event) == [None]


# ---- Guest routes ----


@pytest.fixture
def guest_event(organization: Organization, next_week: datetime) -> Event:
    # Stripe-connected so the ONLINE cart can reserve (no Stripe call until the session step).
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return Event.objects.create(
        organization=organization,
        name="Guest Attribution Event",
        slug="guest-attribution-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        max_tickets_per_user=5,
        can_attend_without_login=True,
        requires_ticket=True,
    )


GUEST = {"email": "guest-attr@example.com", "first_name": "Gia", "last_name": "Guest"}


def _checkout_then_confirm(
    django_capture_on_commit_callbacks: t.Callable[..., t.Any], url: str, payload: dict[str, t.Any]
) -> None:
    """Drive a guest non-online checkout to its confirmed tickets."""
    client = Client()
    with (
        mock.patch("events.tasks.send_guest_ticket_confirmation") as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = client.post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert Ticket.objects.count() == 0, "non-online guest checkout must defer to the confirmation click"

    token = send.delay.call_args.args[1]
    confirm = client.post(reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json")
    assert confirm.status_code == 200, confirm.content


def test_guest_multi_tier_checkout_carries_attribution_through_the_token(
    guest_event: Event, django_capture_on_commit_callbacks: t.Callable[..., t.Any]
) -> None:
    tier = _tier(guest_event, "Free", payment_method=TicketTier.PaymentMethod.FREE, price=Decimal("0.00"))
    payload = {
        **GUEST,
        "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}, {"guest_name": "Bob"}]}],
        "attribution": ATTRIBUTION,
    }
    url = reverse("api:guest_multi_tier_checkout", kwargs={"event_id": guest_event.pk})
    _checkout_then_confirm(django_capture_on_commit_callbacks, url, payload)
    assert _attributions(guest_event) == [ATTRIBUTION] * 2


def test_guest_single_tier_checkout_carries_attribution_through_the_token(
    guest_event: Event, django_capture_on_commit_callbacks: t.Callable[..., t.Any]
) -> None:
    tier = _tier(guest_event, "Free", payment_method=TicketTier.PaymentMethod.FREE, price=Decimal("0.00"))
    payload = {**GUEST, "tickets": [{"guest_name": "Ann"}], "attribution": ATTRIBUTION}
    url = reverse("api:guest_ticket_checkout", kwargs={"event_id": guest_event.pk, "tier_id": tier.pk})
    _checkout_then_confirm(django_capture_on_commit_callbacks, url, payload)
    assert _attributions(guest_event) == [ATTRIBUTION]


def test_guest_pwyc_checkout_carries_attribution_through_the_token(
    guest_event: Event, django_capture_on_commit_callbacks: t.Callable[..., t.Any]
) -> None:
    tier = _tier(
        guest_event,
        "PWYC",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5.00"),
        pwyc_max=Decimal("50.00"),
    )
    payload = {**GUEST, "tickets": [{"guest_name": "Ann"}], "price_per_ticket": "10.00", "attribution": ATTRIBUTION}
    url = reverse("api:guest_ticket_pwyc_checkout", kwargs={"event_id": guest_event.pk, "tier_id": tier.pk})
    _checkout_then_confirm(django_capture_on_commit_callbacks, url, payload)
    assert _attributions(guest_event) == [ATTRIBUTION]


def test_guest_online_checkout_stamps_pending_tickets(guest_event: Event) -> None:
    tier = _tier(guest_event, "Online", payment_method=TicketTier.PaymentMethod.ONLINE)
    payload = {
        **GUEST,
        "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}],
        "attribution": ATTRIBUTION,
    }
    url = reverse("api:guest_multi_tier_checkout", kwargs={"event_id": guest_event.pk})
    response = Client().post(url, data=payload, content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["requires_payment"] is True

    ticket = Ticket.objects.get(event=guest_event)
    assert ticket.status == Ticket.TicketStatus.PENDING
    assert ticket.attribution == ATTRIBUTION
