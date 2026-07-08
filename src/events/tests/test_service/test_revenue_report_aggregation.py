"""Aggregation tests for the revenue & VAT report (#551)."""

import datetime as dt
import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import revenue_report_service as svc


@pytest.fixture
def org_event_tier(db: t.Any) -> tuple[Organization, Event, TicketTier, RevelUser]:
    user = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(
        name="Org", slug="org", owner=user, vat_rate=Decimal("20.00"), vat_country_code="AT"
    )
    now = timezone.now()
    event = Event.objects.create(
        organization=org,
        name="E",
        slug="e",
        start=now,
        end=now + dt.timedelta(hours=2),
    )
    tier = TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("120.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    return org, event, tier, user


def _scope(org: Organization) -> svc.ReportScope:
    # Wide window so rows stamped at ``timezone.now()`` (auto ``created_at``,
    # ``refunded_at``, ``cancelled_at``) always fall in-period regardless of the
    # year the suite runs in. These tests do not exercise out-of-period exclusion.
    return svc.ReportScope(org=org, event_id=None, date_from=dt.date(2000, 1, 1), date_to=dt.date(2100, 1, 1))


@pytest.mark.django_db
def test_single_succeeded_payment_splits_vat_at_stored_rate(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    org, event, tier, user = org_event_tier
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Alice"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("20.00"),
        vat_rate=Decimal("20.00"),
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_1",
    )
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    bucket = next(b for b in section.rate_buckets if b.vat_rate == Decimal("20.00"))
    assert bucket.net == Decimal("100.00")
    assert bucket.vat == Decimal("20.00")
    assert bucket.gross == Decimal("120.00")
    assert section.net_taxable_turnover == Decimal("100.00")
    assert section.sold_count == 1


@pytest.mark.django_db
def test_null_vat_fields_are_derived_from_org_rate(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    org, event, tier, user = org_event_tier
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Bob"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_2",
    )  # net/vat/rate left null
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    bucket = next(b for b in section.rate_buckets if b.vat_rate == Decimal("20.00"))
    assert bucket.net == Decimal("100.00")  # 120 / 1.20
    assert bucket.vat == Decimal("20.00")


@pytest.mark.django_db
def test_refund_reduces_bucket_and_reports_refund_total(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    org, event, tier, user = org_event_tier
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.CANCELLED, guest_name="Carol"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.REFUNDED,
        amount=Decimal("120.00"),
        currency="EUR",
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("20.00"),
        vat_rate=Decimal("20.00"),
        refund_amount=Decimal("120.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
        refunded_at=timezone.now(),
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_3",
    )
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    assert section.refunds_total == Decimal("120.00")
    assert section.net_taxable_turnover == Decimal("0.00")
    assert section.sold_count == 1
    assert section.refunded_count == 1


@pytest.mark.django_db
def test_vat_rate_bucket_label_is_human_readable(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    """A 20% rate renders as "20%", not Decimal scientific notation "2E+1%" (#554)."""
    org, event, tier, user = org_event_tier
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Zoe"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("20.00"),
        vat_rate=Decimal("20.00"),
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_label",
    )
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    labels = [b.label for b in section.rate_buckets]
    assert "20%" in labels
    assert "2E+1%" not in labels


@pytest.mark.django_db
def test_offline_zero_refund_still_increments_refunded_count(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    """An offline ticket cancelled with a 0 refund is counted as refunded (#554 review)."""
    org, event, _, user = org_event_tier
    offline_tier = TicketTier.objects.create(
        event=event,
        name="Door",
        price=Decimal("60.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )
    Ticket.objects.create(
        event=event,
        tier=offline_tier,
        user=user,
        status=Ticket.TicketStatus.CANCELLED,
        guest_name="Bob",
        offline_refund_amount=Decimal("0.00"),
        cancelled_at=timezone.now(),
    )
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    assert section.refunded_count == 1
    assert section.refunds_total == Decimal("0.00")


@pytest.mark.django_db
def test_zero_price_paid_ticket_on_offline_tier_reports_zero_not_tier_price(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    """A series-pass-materialized ticket mapped onto an OFFLINE tier (a FREE pass, or an
    extension/backfill grant) records its real ``price_paid`` of 0 — the aggregator must
    report 0 turnover for it, never the mapped tier's full price (#644)."""
    org, event, _, user = org_event_tier
    offline_tier = TicketTier.objects.create(
        event=event,
        name="Door",
        price=Decimal("60.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )
    Ticket.objects.create(
        event=event,
        tier=offline_tier,
        user=user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Pass Holder",
        price_paid=Decimal("0.00"),
    )
    data = svc.build_revenue_report_data(_scope(org))
    section = next(s for s in data.sections if s.currency == "EUR")
    assert section.net_taxable_turnover == Decimal("0.00")
    assert section.sold_count == 1


@pytest.mark.django_db
def test_empty_period_returns_empty_sections(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    org, _, _, _ = org_event_tier
    data = svc.build_revenue_report_data(_scope(org))
    assert data.sections == []


@pytest.mark.django_db
def test_data_hash_changes_when_a_refund_is_recorded(
    org_event_tier: tuple[Organization, Event, TicketTier, RevelUser],
) -> None:
    org, event, tier, user = org_event_tier
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Dave"
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_4",
    )
    before = svc.compute_revenue_data_hash(_scope(org))
    payment.status = Payment.PaymentStatus.REFUNDED
    payment.refund_amount = Decimal("120.00")
    payment.refund_status = Payment.RefundStatus.SUCCEEDED
    payment.save()
    after = svc.compute_revenue_data_hash(_scope(org))
    assert before != after
