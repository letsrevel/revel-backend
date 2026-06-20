"""Cache behaviour for the revenue report (#551)."""

import datetime as dt
import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.models import FileExport
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import revenue_report_service as svc


@pytest.fixture
def org_scope(db: t.Any) -> tuple[Organization, RevelUser, svc.ReportScope]:
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
        event=event, name="GA", price=Decimal("120.00"), currency="EUR", payment_method=TicketTier.PaymentMethod.ONLINE
    )
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Alice"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_cache_1",
    )
    # Wide window so the now-stamped sale always falls in-period (year-agnostic).
    scope = svc.ReportScope(org=org, event_id=None, date_from=dt.date(2000, 1, 1), date_to=dt.date(2100, 1, 1))
    return org, user, scope


@pytest.mark.django_db
def test_generate_then_cache_hit_reuses_export(org_scope: t.Any) -> None:
    org, user, scope = org_scope
    export = svc.get_or_generate_revenue_report(org, scope, requested_by=user)
    svc.generate_revenue_report(export.id)  # run synchronously
    export.refresh_from_db()
    assert export.status == FileExport.ExportStatus.READY

    again = svc.get_or_generate_revenue_report(org, scope, requested_by=user)
    assert again.id == export.id  # cache hit


@pytest.mark.django_db
def test_refresh_forces_a_new_export(org_scope: t.Any) -> None:
    org, user, scope = org_scope
    first = svc.get_or_generate_revenue_report(org, scope, requested_by=user)
    svc.generate_revenue_report(first.id)
    second = svc.get_or_generate_revenue_report(org, scope, requested_by=user, refresh=True)
    assert second.id != first.id


@pytest.mark.django_db
def test_data_hash_changes_when_vat_rate_changes(org_scope: t.Any) -> None:
    org, _user, scope = org_scope
    hash_before = svc.compute_revenue_data_hash(scope)
    org.vat_rate = Decimal("13.00")
    org.save(update_fields=["vat_rate"])
    hash_after = svc.compute_revenue_data_hash(scope)
    assert hash_before != hash_after


@pytest.mark.django_db
def test_new_payment_invalidates_cache(org_scope: t.Any) -> None:
    org, user, scope = org_scope
    first = svc.get_or_generate_revenue_report(org, scope, requested_by=user)
    svc.generate_revenue_report(first.id)
    event = Event.objects.get(organization=org)
    tier = TicketTier.objects.get(event=event, name="GA")
    ticket2 = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Bob"
    )
    Payment.objects.create(
        ticket=ticket2,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("60.00"),
        currency="EUR",
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_cache_2",
    )
    nxt = svc.get_or_generate_revenue_report(org, scope, requested_by=user)
    assert nxt.id != first.id  # data_hash changed → miss
