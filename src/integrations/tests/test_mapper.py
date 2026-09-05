"""Revel → neutral mapping: eligibility, summary, tier skip rules, hidden derivation, currency majority."""

from decimal import Decimal

import pytest
from django.conf import settings

from events.models import Event, MembershipTier, TicketTier
from integrations.schema import IntegrationErrorCode
from integrations.service import mapper
from integrations.service.mapper import EventNotEligible

pytestmark = pytest.mark.django_db


def _tier(event: Event, name: str, **kw: object) -> TicketTier:
    defaults: dict[str, object] = {
        "price": Decimal("10"),
        "total_quantity": 100,
        "payment_method": TicketTier.PaymentMethod.ONLINE,
    }
    defaults.update(kw)
    return TicketTier.objects.create(event=event, name=name, **defaults)


@pytest.fixture
def clean_event(event: Event) -> Event:
    event.ticket_tiers.all().delete()  # drop the signal-created "General Admission"
    return event


def test_eligibility_rejects_private_open_ended_and_no_tickets(clean_event: Event) -> None:
    clean_event.event_type = Event.EventType.PRIVATE
    with pytest.raises(EventNotEligible) as exc:
        mapper.check_eligible(clean_event)
    assert exc.value.code == IntegrationErrorCode.EVENT_PRIVATE
    clean_event.event_type = Event.EventType.PUBLIC
    clean_event.is_open_ended = True
    with pytest.raises(EventNotEligible) as exc:
        mapper.check_eligible(clean_event)
    assert exc.value.code == IntegrationErrorCode.EVENT_OPEN_ENDED
    clean_event.is_open_ended = False
    clean_event.requires_ticket = False
    with pytest.raises(EventNotEligible) as exc:
        mapper.check_eligible(clean_event)
    assert exc.value.code == IntegrationErrorCode.EVENT_NO_TICKETS
    clean_event.requires_ticket = True
    mapper.check_eligible(clean_event)  # no raise


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        (None, ""),
        # Non-heading blocks (p/li/blockquote) win over headings — a title is a worse
        # public summary than the first real sentence that follows it.
        ("# Title\n\nFirst sentence here. Second one.", "First sentence here."),
        ("**Bold** start! Then more", "Bold start!"),
        ("x" * 200, "x" * 139 + "…"),
        ("## Heading\n- item one\n- item two", "item one"),
        ("## Only a heading", "Only a heading"),
        ("- item one. More\n- item two", "item one."),
        ("plain text\nwith newline", "plain text with newline"),
    ],
)
def test_summary_from_markdown(md: str | None, expected: str) -> None:
    out = mapper.summary_from_markdown(md)
    assert out == expected
    assert len(out) <= mapper.SUMMARY_MAX_CHARS


def test_timezone_prefers_city_then_settings(clean_event: Event) -> None:
    assert mapper.event_timezone(clean_event) == settings.TIME_ZONE
    from django.contrib.gis.geos import Point

    from geo.models import City

    city = City.objects.create(
        name="Wien",
        ascii_name="Wien",
        country="Austria",
        city_id=1,
        location=Point(16.37, 48.21, srid=4326),
        timezone="Europe/Vienna",
    )
    clean_event.city = city
    assert mapper.event_timezone(clean_event) == "Europe/Vienna"


def test_map_event_basic_fields_and_venue(clean_event: Event) -> None:
    from django.contrib.gis.geos import Point

    from geo.models import City

    city = City.objects.create(
        name="Wien",
        ascii_name="Wien",
        country="Austria",
        iso2="at",
        city_id=2,
        location=Point(16.37, 48.21, srid=4326),
    )
    clean_event.description = "Great **night**. Bring friends."
    clean_event.address = "Stephansplatz 1, 1010 Wien"
    clean_event.city = city
    clean_event.save()
    clean_event.ticket_tiers.all().delete()  # save() with no tiers re-triggers the default-tier signal
    _tier(clean_event, "GA")
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    r = mapped.remote
    assert r.name == clean_event.name and r.start == clean_event.start and r.end == clean_event.end
    assert r.summary == "Great night."
    assert "<strong>night</strong>" in r.description_html
    assert r.currency == "EUR" and r.status == "draft" and r.is_virtual is False
    assert r.venue is not None and r.venue.address == "Stephansplatz 1, 1010 Wien" and r.venue.name == clean_event.name
    assert r.venue.country == "AT"
    assert r.ticket_classes == []
    assert [m.remote.name for m in mapped.tiers] == ["GA"]
    assert [e.code for e in mapped.report] == [IntegrationErrorCode.IMAGE_MISSING]


@pytest.mark.parametrize(
    ("visibility", "listed"),
    [(Event.Visibility.PUBLIC, True), (Event.Visibility.UNLISTED, False), (Event.Visibility.PRIVATE, False)],
)
def test_listed_follows_revel_visibility(clean_event: Event, visibility: str, listed: bool) -> None:
    clean_event.visibility = visibility
    clean_event.save()
    clean_event.ticket_tiers.all().delete()  # save() with no tiers re-triggers the default-tier signal
    _tier(clean_event, "GA")
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    assert mapped.remote.listed is listed


def test_virtual_event_has_no_venue(clean_event: Event) -> None:
    clean_event.is_virtual = True
    clean_event.save()
    _tier(clean_event, "GA")
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    assert mapped.remote.is_virtual is True and mapped.remote.venue is None


def test_tier_skip_rules(clean_event: Event) -> None:
    mt = MembershipTier.objects.create(organization=clean_event.organization, name="Gold")
    _tier(clean_event, "PWYC", price_type=TicketTier.PriceType.PWYC, pwyc_min=Decimal("1"))
    members = _tier(clean_event, "Members")
    members.restricted_to_membership_tiers.add(mt)
    seated = _tier(clean_event, "Seated")
    # BEST_AVAILABLE requires a sector at the model-validation level; the mapper only cares
    # about the field value, so set it directly via the queryset to skip full_clean().
    TicketTier.objects.filter(pk=seated.pk).update(seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE)
    _tier(clean_event, "Door", payment_method=TicketTier.PaymentMethod.AT_THE_DOOR)
    _tier(clean_event, "Unlimited", total_quantity=None)
    _tier(clean_event, "USD", currency="USD")
    _tier(clean_event, "OK")
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    assert [m.remote.name for m in mapped.tiers] == ["OK"]
    codes = {e.tier_name: e.code for e in mapped.report if e.scope == "tier"}
    assert codes == {
        "PWYC": IntegrationErrorCode.TIER_VARIABLE_PRICE,
        "Members": IntegrationErrorCode.TIER_MEMBERS_ONLY,
        "Seated": IntegrationErrorCode.TIER_SEATED,
        "Door": IntegrationErrorCode.TIER_OFFLINE_PAYMENT,
        "Unlimited": IntegrationErrorCode.TIER_NO_CAPACITY,
        "USD": IntegrationErrorCode.TIER_CURRENCY_MISMATCH,
    }
    assert all(e.tier_id is not None for e in mapped.report if e.scope == "tier")


def test_unlimited_tier_uses_max_attendees(clean_event: Event) -> None:
    clean_event.max_attendees = 250
    clean_event.save()
    _tier(clean_event, "Unlimited", total_quantity=None)
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    assert mapped.tiers[0].remote.quantity_total == 250


def test_hidden_and_free_and_remote_ids(clean_event: Event) -> None:
    unlisted = _tier(clean_event, "Unlisted", visibility=TicketTier.Visibility.UNLISTED)
    _tier(clean_event, "Paused", sales_paused=True)
    remote_paused = _tier(clean_event, "RemotePaused")
    free = _tier(clean_event, "Free", price=Decimal("0"), payment_method=TicketTier.PaymentMethod.FREE)
    mapped = mapper.map_event(
        clean_event,
        remote_paused={remote_paused.id: True},
        remote_tier_ids={free.id: "tc-9"},
    )
    by_name = {m.remote.name: m.remote for m in mapped.tiers}
    assert by_name["Unlisted"].hidden and by_name["Paused"].hidden and by_name["RemotePaused"].hidden
    assert by_name["Free"].hidden is False and by_name["Free"].is_free is True and by_name["Free"].remote_id == "tc-9"
    assert by_name["Unlisted"].remote_id is None
    assert unlisted.id in {m.tier.id for m in mapped.tiers}


def test_currency_majority_ties_break_toward_first_tier(clean_event: Event) -> None:
    _tier(clean_event, "A", currency="USD", display_order=0)
    _tier(clean_event, "B", currency="EUR", display_order=1)
    mapped = mapper.map_event(clean_event, remote_paused={}, remote_tier_ids={})
    assert mapped.remote.currency == "USD"
    assert [e.tier_name for e in mapped.report if e.code == IntegrationErrorCode.TIER_CURRENCY_MISMATCH] == ["B"]
