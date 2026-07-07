import typing as t
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from events.models import Event, EventSeries, HeldSeriesPass, Organization, SeriesPass, SeriesPassTierLink, TicketTier


@pytest.mark.django_db
class TestSeriesPassModels:
    def test_create_series_pass(self, organization: Organization, event_series: EventSeries) -> None:
        sp = SeriesPass.objects.create(
            event_series=event_series,
            name="Season Ticket",
            price=Decimal("36.00"),
            pro_rata_discount=Decimal("6.00"),
            currency="EUR",
        )
        assert sp.is_active is True
        assert sp.payment_method == TicketTier.PaymentMethod.ONLINE
        assert sp.purchasable_by == TicketTier.PurchasableBy.PUBLIC

    def test_at_the_door_rejected(self, event_series: EventSeries) -> None:
        sp = SeriesPass(
            event_series=event_series,
            name="Bad",
            price=Decimal("10.00"),
            pro_rata_discount=Decimal("1.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        )
        with pytest.raises(ValidationError):
            sp.full_clean()

    def test_tier_link_unique_per_event(self, series_pass: SeriesPass, event: Event, ticket_tier: TicketTier) -> None:
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)
        # TimeStampedModel.save() runs full_clean(), and Django's validate_unique() checks
        # unconditioned UniqueConstraints before hitting the DB — so the duplicate is caught
        # as a ValidationError here, never reaching the DB to raise IntegrityError. The DB
        # constraint (unique_series_pass_event) still exists and is exercised via bulk_create,
        # which skips full_clean entirely.
        with pytest.raises(ValidationError):
            SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)
        with pytest.raises(IntegrityError):
            SeriesPassTierLink.objects.bulk_create(
                [SeriesPassTierLink(series_pass=series_pass, event=event, tier=ticket_tier)]
            )

    def test_tier_link_tier_must_belong_to_event(
        self, series_pass: SeriesPass, event: Event, other_event_tier: TicketTier
    ) -> None:
        link = SeriesPassTierLink(series_pass=series_pass, event=event, tier=other_event_tier)
        with pytest.raises(ValidationError):
            link.full_clean()

    def test_held_pass_defaults(self, series_pass: SeriesPass, revel_user: t.Any) -> None:
        hp = HeldSeriesPass.objects.create(series_pass=series_pass, user=revel_user, price_paid=Decimal("36.00"))
        assert hp.status == HeldSeriesPass.Status.PENDING
