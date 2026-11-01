"""RSVP-only events (``requires_ticket=False``) sell nothing and the flag is create-only (#1295)."""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from pydantic import ValidationError

from events.models import Event, Organization, RecurrenceRule, TicketTier
from events.schema.recurring_event import TemplateEditSchema
from events.service import recurrence_service
from events.service.recurrence_service import PROPAGATABLE_FIELDS, PropagateScope

pytestmark = pytest.mark.django_db

RSVP_ONLY_DETAIL = "This event is RSVP-only and does not sell tickets."


def _rsvp_event(public_event: Event) -> Event:
    public_event.requires_ticket = False
    public_event.can_attend_without_login = True
    public_event.save(update_fields=["requires_ticket", "can_attend_without_login"])
    return public_event


def _tier(event: Event, name: str = "Legacy", **kwargs: t.Any) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        total_quantity=100,
        **kwargs,
    )


def _post(client: Client, url: str, data: dict[str, t.Any]) -> t.Any:
    return client.post(url, data=orjson.dumps(data), content_type="application/json")


def _assert_rsvp_only(response: t.Any) -> None:
    assert response.status_code == 400, response.content
    assert response.json()["detail"] == RSVP_ONLY_DETAIL


class TestTierCreation:
    def test_rejected_on_rsvp_only_event(self, organization_owner_client: Client, public_event: Event) -> None:
        event = _rsvp_event(public_event)
        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        before = TicketTier.objects.filter(event=event).count()

        _assert_rsvp_only(_post(organization_owner_client, url, {"name": "VIP", "price": "10.00"}))

        assert TicketTier.objects.filter(event=event).count() == before

    def test_allowed_on_ticketed_event(self, organization_owner_client: Client, public_event: Event) -> None:
        url = reverse("api:create_ticket_tier", kwargs={"event_id": public_event.pk})

        response = _post(organization_owner_client, url, {"name": "VIP", "price": "10.00"})

        assert response.status_code == 200, response.content


class TestDuplication:
    def test_duplicate_rsvp_only_event_copies_no_tiers(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        _rsvp_event(public_event)
        _tier(public_event)
        url = reverse("api:duplicate_event", kwargs={"event_id": public_event.pk})
        payload = {"name": "RSVP Copy", "start": (public_event.start + timedelta(days=7)).isoformat()}

        response = _post(organization_owner_client, url, payload)

        assert response.status_code == 200, response.content
        new_event = Event.objects.get(pk=response.json()["id"])
        assert new_event.requires_ticket is False
        assert new_event.ticket_tiers.count() == 0


class TestCheckoutRefused:
    def test_auth_single_tier(self, member_client: Client, public_event: Event) -> None:
        event = _rsvp_event(public_event)
        tier = _tier(event)
        url = reverse("api:ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})

        _assert_rsvp_only(_post(member_client, url, {"tickets": [{"guest_name": "Ann"}]}))

    def test_auth_pwyc(self, member_client: Client, public_event: Event) -> None:
        event = _rsvp_event(public_event)
        tier = _tier(event, price_type=TicketTier.PriceType.PWYC, pwyc_min=Decimal("1.00"))
        url = reverse("api:ticket_pwyc_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})

        _assert_rsvp_only(_post(member_client, url, {"tickets": [{"guest_name": "Ann"}], "price_per_ticket": "5.00"}))

    def test_auth_multi_tier(self, member_client: Client, public_event: Event) -> None:
        event = _rsvp_event(public_event)
        tier = _tier(event)
        url = reverse("api:multi_tier_checkout", kwargs={"event_id": event.pk})

        _assert_rsvp_only(_post(member_client, url, {"items": [{"tier_id": str(tier.pk), "tickets": [{}]}]}))

    @pytest.mark.parametrize(
        "route", ["guest_ticket_checkout", "guest_ticket_pwyc_checkout", "guest_multi_tier_checkout"]
    )
    def test_guest_routes(self, client: Client, public_event: Event, route: str) -> None:
        event = _rsvp_event(public_event)
        extra: dict[str, t.Any] = {"price_type": TicketTier.PriceType.PWYC, "pwyc_min": Decimal("1.00")}
        tier = _tier(event, **(extra if route == "guest_ticket_pwyc_checkout" else {}))
        guest = {"email": "guest@example.com", "first_name": "G", "last_name": "Uest"}
        if route == "guest_multi_tier_checkout":
            url = reverse(f"api:{route}", kwargs={"event_id": event.pk})
            data = {**guest, "items": [{"tier_id": str(tier.pk), "tickets": [{"guest_name": "G"}]}]}
        else:
            url = reverse(f"api:{route}", kwargs={"event_id": event.pk, "tier_id": tier.pk})
            data = {**guest, "tickets": [{"guest_name": "G"}], "price_per_ticket": "5.00"}

        _assert_rsvp_only(_post(client, url, data))

    def test_guest_rsvp_only_error_wins_over_login_required(self, client: Client, public_event: Event) -> None:
        event = _rsvp_event(public_event)
        event.can_attend_without_login = False
        event.save(update_fields=["can_attend_without_login"])
        tier = _tier(event)
        url = reverse("api:guest_ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})
        data = {"email": "guest@example.com", "first_name": "G", "last_name": "Uest", "tickets": [{"guest_name": "G"}]}

        _assert_rsvp_only(_post(client, url, data))


class TestRequiresTicketIsCreateOnly:
    @freeze_time("2030-03-25 00:00:00")
    def test_template_edit_cannot_change_it(self, organization: Organization) -> None:
        dtstart = timezone.make_aware(datetime(2030, 4, 1, 10, 0))
        series = recurrence_service.create_recurring_event_series(
            organization=organization,
            recurrence_data={
                "frequency": RecurrenceRule.Frequency.WEEKLY,
                "interval": 1,
                "weekdays": [dtstart.weekday()],
                "dtstart": dtstart,
            },
            series_name="Weekly",
            series_description="",
            auto_publish=False,
            generation_window_weeks=3,
            event_data={
                "name": "Weekly",
                "start": dtstart,
                "end": dtstart + timedelta(hours=2),
                "status": Event.EventStatus.DRAFT,
                "visibility": Event.Visibility.PUBLIC,
                "event_type": Event.EventType.PUBLIC,
                "requires_ticket": False,
            },
        )
        assert "requires_ticket" not in TemplateEditSchema.model_fields
        assert "requires_ticket" not in PROPAGATABLE_FIELDS

        with pytest.raises(ValidationError):  # extra="forbid": the flag is refused outright
            TemplateEditSchema.model_validate({"requires_ticket": True})
        payload = TemplateEditSchema(description="New")
        updated = recurrence_service.update_template(series=series, payload=payload, scope=PropagateScope.ALL_FUTURE)

        assert updated.template_event is not None
        assert updated.template_event.description == "New"
        assert updated.template_event.requires_ticket is False
        occurrences = list(series.events.filter(is_template=False))
        assert occurrences
        assert all(e.description == "New" and e.requires_ticket is False for e in occurrences)
