"""Per-country decisions of the layer-1 policy table (#1057-#1067): minimal, scoped, dated."""

import pytest
from django.utils import translation
from freezegun import freeze_time

from events.compliance import (
    AttendeeInvoicingCapability,
    BuyerContext,
    Nexus,
    PaymentChannelCapability,
    get_policy_for_country,
)
from events.compliance.base import ALL_NEXUS, country_name
from events.compliance.enforcement import attendee_invoicing_for_sale

EST = frozenset({Nexus.ESTABLISHMENT})
VENUE = frozenset({Nexus.VENUE})

INVOICING_BLOCKED = ["HR", "PT", "SI", "GR", "RO", "HU"]
B2B_BLOCKED = ["BE", "PL"]
UNRESTRICTED = ["AT", "DE", "FR", "IT", "NL", "US", ""]


@pytest.mark.parametrize("code", INVOICING_BLOCKED)
def test_fiscalized_countries_block_every_attendee_invoice(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED
    for buyer in (BuyerContext(), BuyerContext(vat_country=code), BuyerContext(vat_country="DE")):
        decision = policy.attendee_invoicing(buyer, EST)
        assert not decision.allowed
        assert country_name(code) in decision.reason


@pytest.mark.parametrize(
    ("code", "binds_venue"), [("HR", False), ("PT", False), ("RO", False), ("SI", True), ("GR", True), ("HU", True)]
)
def test_fiscalized_invoicing_reaches_venues_only_where_the_law_does(code: str, binds_venue: bool) -> None:
    """HR/PT/RO bind established sellers; SI/GR/HU also foreign sellers of events held there."""
    assert get_policy_for_country(code).attendee_invoicing(BuyerContext(), VENUE).allowed is not binds_venue


@pytest.mark.parametrize("code", B2B_BLOCKED)
def test_e_invoicing_countries_block_only_domestic_business_buyers_of_established_sellers(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS
    domestic = BuyerContext.from_vat_id(f"{code}0123456789")
    assert policy.attendee_invoicing(BuyerContext(), EST).allowed  # consumer
    assert policy.attendee_invoicing(BuyerContext(vat_country="DE"), EST).allowed  # cross-border B2B
    assert policy.attendee_invoicing(domestic, VENUE).allowed  # seller not established here
    refused = policy.attendee_invoicing(domestic, EST)
    assert not refused.allowed
    assert refused.reason


@pytest.mark.parametrize("code", UNRESTRICTED)
def test_unrestricted_countries_allow_invoicing(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.ALLOWED
    assert policy.attendee_invoicing(BuyerContext(vat_country=code), ALL_NEXUS).allowed


class TestSpainEffectiveFrom:
    @freeze_time("2026-12-31 22:00:00")
    def test_allowed_before_verifactu(self) -> None:
        policy = get_policy_for_country("ES")
        assert policy.attendee_invoicing(BuyerContext(), EST).allowed
        assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.ALLOWED

    @freeze_time("2027-01-01 12:00:00")
    def test_blocked_from_2027(self) -> None:
        policy = get_policy_for_country("ES")
        decision = policy.attendee_invoicing(BuyerContext(), EST)
        assert not decision.allowed
        assert "Verifactu" in decision.reason
        assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED


class TestItalyOnlinePayment:
    def test_online_checkout_blocked_for_events_held_in_italy(self) -> None:
        policy = get_policy_for_country("IT")
        decision = policy.online_payment(VENUE)
        assert not decision.allowed
        assert "Online card payments" in decision.reason
        assert "payment at the door or by bank transfer" in decision.reason
        assert policy.online_payment_capability() == PaymentChannelCapability.BLOCKED

    def test_italian_org_selling_abroad_is_not_reached(self) -> None:
        assert get_policy_for_country("IT").online_payment(EST).allowed

    def test_offline_payment_and_invoicing_stay_allowed(self) -> None:
        policy = get_policy_for_country("IT")
        assert policy.offline_payment(ALL_NEXUS).allowed
        assert policy.offline_payment_capability() == PaymentChannelCapability.ALLOWED
        assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.ALLOWED


def test_only_italy_restricts_a_payment_channel() -> None:
    for code in [*INVOICING_BLOCKED, *B2B_BLOCKED, "ES", "FR", "AT", ""]:
        policy = get_policy_for_country(code)
        assert policy.online_payment_capability() == PaymentChannelCapability.ALLOWED, code
        assert policy.offline_payment_capability() == PaymentChannelCapability.ALLOWED, code


class TestStrictestReachingCountryWins:
    def test_any_blocking_country_blocks_the_sale(self) -> None:
        assert not attendee_invoicing_for_sale({"AT": EST, "HR": EST}, None).allowed

    def test_venue_only_reach_respects_the_policy_scope(self) -> None:
        assert attendee_invoicing_for_sale({"AT": EST, "HR": VENUE}, None).allowed
        assert not attendee_invoicing_for_sale({"AT": EST, "SI": VENUE}, None).allowed

    def test_b2b_block_needs_an_established_seller_and_domestic_vat_id(self) -> None:
        assert attendee_invoicing_for_sale({"BE": EST}, "DE123456789").allowed
        assert attendee_invoicing_for_sale({"BE": EST}, "").allowed
        assert attendee_invoicing_for_sale({"AT": EST, "BE": VENUE}, "BE0123456789").allowed
        assert not attendee_invoicing_for_sale({"BE": EST}, "BE0123456789").allowed

    def test_greek_vat_prefix_is_normalized(self) -> None:
        assert not attendee_invoicing_for_sale({"GR": EST}, "EL123456789").allowed


def test_refusals_name_the_country_in_the_active_language() -> None:
    with translation.override("it"):
        reason = get_policy_for_country("HR").attendee_invoicing(BuyerContext(), EST).reason
        assert country_name("HR") == "Croazia"
    assert "Croazia" in reason
    assert country_name("XX") == "XX"
