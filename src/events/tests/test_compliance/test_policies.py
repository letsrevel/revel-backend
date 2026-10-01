"""Per-country decisions of the layer-1 policy table (#1057-#1067)."""

import pytest

from events.compliance import (
    AttendeeInvoicingCapability,
    BuyerContext,
    PaidTicketingCapability,
    get_policy_for_country,
)
from events.compliance.enforcement import attendee_invoicing_for_sale

INVOICING_BLOCKED = ["HR", "ES", "PT", "SI", "GR", "RO", "HU"]
B2B_BLOCKED = ["BE", "PL"]
UNRESTRICTED = ["AT", "DE", "FR", "IT", "NL", "US", ""]


@pytest.mark.parametrize("code", INVOICING_BLOCKED)
def test_fiscalized_countries_block_every_attendee_invoice(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability == AttendeeInvoicingCapability.BLOCKED
    for buyer in (BuyerContext(), BuyerContext(vat_country=code), BuyerContext(vat_country="DE")):
        decision = policy.attendee_invoicing(buyer)
        assert not decision.allowed
        assert code in decision.reason


@pytest.mark.parametrize("code", B2B_BLOCKED)
def test_e_invoicing_countries_block_only_domestic_business_buyers(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability == AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS
    assert policy.attendee_invoicing(BuyerContext()).allowed  # consumer
    assert policy.attendee_invoicing(BuyerContext(vat_country="DE")).allowed  # cross-border B2B
    domestic = policy.attendee_invoicing(BuyerContext.from_vat_id(f"{code}0123456789"))
    assert not domestic.allowed
    assert domestic.reason


@pytest.mark.parametrize("code", UNRESTRICTED)
def test_unrestricted_countries_allow_invoicing(code: str) -> None:
    policy = get_policy_for_country(code)
    assert policy.attendee_invoicing_capability == AttendeeInvoicingCapability.ALLOWED
    assert policy.attendee_invoicing(BuyerContext(vat_country=code)).allowed


def test_only_italy_blocks_paid_ticketing() -> None:
    for code in [*INVOICING_BLOCKED, *B2B_BLOCKED, *UNRESTRICTED]:
        policy = get_policy_for_country(code)
        allowed = code != "IT"
        assert policy.paid_ticketing().allowed is allowed, code
        assert (policy.paid_ticketing_capability == PaidTicketingCapability.ALLOWED) is allowed, code
    decision = get_policy_for_country("IT").paid_ticketing()
    assert not decision.allowed
    assert "IT" in decision.reason


class TestStrictestLiableCountryWins:
    def test_any_blocking_country_blocks_the_sale(self) -> None:
        assert not attendee_invoicing_for_sale(["AT", "ES"], None).allowed

    def test_b2b_block_needs_a_domestic_vat_id(self) -> None:
        assert attendee_invoicing_for_sale(["AT", "BE"], "DE123456789").allowed
        assert attendee_invoicing_for_sale(["AT", "BE"], "").allowed
        assert not attendee_invoicing_for_sale(["AT", "BE"], "BE0123456789").allowed

    def test_unrestricted_countries_allow(self) -> None:
        assert attendee_invoicing_for_sale(["AT", "", "DE"], "DE123456789").allowed

    def test_greek_vat_prefix_is_normalized(self) -> None:
        assert not attendee_invoicing_for_sale(["EL"], None).allowed
