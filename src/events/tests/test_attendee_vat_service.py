"""Tests for attendee VAT calculation logic (phase 1 of #868).

Admission to events is taxed where the event takes place (Art. 53/54(1) VAT
Directive), so it is a domestic supply of the organizer: every buyer pays the
gross price at the seller's rate. Reverse charge and non-EU export zero-rating
never apply to admission.

Tests cover:
- determine_attendee_vat() — the identical gross-at-seller-rate result for
  every buyer profile (domestic/EU/non-EU, with or without a validated VAT ID)
- get_effective_vat_rate() tier override vs org fallback
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from events.service.attendee_vat_service import (
    determine_attendee_vat,
    get_effective_vat_rate,
)

# ---------------------------------------------------------------------------
# determine_attendee_vat — every buyer profile pays gross at the seller rate
# ---------------------------------------------------------------------------


class TestDetermineAttendeeVatDomesticB2C:
    """Domestic B2C: same country, no VAT ID -> full VAT."""

    def test_domestic_b2c_charges_full_vat(self) -> None:
        """When buyer is in the same country with no VAT ID, full VAT applies."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=False,
        )

        assert result.effective_price == Decimal("122.00")
        assert result.vat_rate == Decimal("22.00")
        assert result.reverse_charge is False
        assert result.net_amount + result.vat_amount == result.effective_price

    def test_domestic_b2c_vat_breakdown_is_correct(self) -> None:
        """Net + VAT must equal the gross price for domestic B2C."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="DE",
            buyer_country="DE",
            buyer_vat_id_valid=False,
        )

        # 100 / 1.22 = 81.97 (rounded), VAT = 18.03
        assert result.net_amount == Decimal("81.97")
        assert result.vat_amount == Decimal("18.03")
        assert result.effective_price == Decimal("100.00")


class TestDetermineAttendeeVatDomesticB2B:
    """Domestic B2B: same country, valid VAT ID -> still full VAT."""

    def test_domestic_b2b_charges_full_vat(self) -> None:
        """When buyer is in the same country WITH a valid VAT ID, full VAT still applies."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=True,
        )

        assert result.effective_price == Decimal("122.00")
        assert result.vat_rate == Decimal("22.00")
        assert result.reverse_charge is False

    def test_domestic_b2b_same_as_b2c(self) -> None:
        """Domestic B2B and B2C should yield identical results."""
        params: dict[str, t.Any] = {
            "gross_price": Decimal("50.00"),
            "seller_vat_rate": Decimal("19.00"),
            "seller_country": "DE",
            "buyer_country": "DE",
        }
        b2c = determine_attendee_vat(**params, buyer_vat_id_valid=False)
        b2b = determine_attendee_vat(**params, buyer_vat_id_valid=True)

        assert b2c == b2b


class TestDetermineAttendeeVatEUCrossBorderB2B:
    """EU cross-border B2B: different EU country, valid VAT ID -> still full VAT (#868).

    Admission is taxed where the event takes place, so a validated VAT ID never
    converts the sale into a reverse-charge supply — the ID only feeds invoice
    display.
    """

    def test_eu_cross_border_b2b_charges_full_vat(self) -> None:
        """Buyer in a different EU country with valid VAT ID pays full gross at the seller rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )

        assert result.reverse_charge is False
        assert result.effective_price == Decimal("122.00")
        assert result.vat_rate == Decimal("22.00")
        assert result.vat_amount > Decimal("0.00")
        assert result.net_amount + result.vat_amount == result.effective_price

    def test_eu_cross_border_b2b_vat_breakdown_is_correct(self) -> None:
        """The gross stays gross: net + VAT reconcile at the seller's rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="FR",
            buyer_vat_id_valid=True,
        )

        assert result.effective_price == Decimal("100.00")
        assert result.net_amount == Decimal("81.97")
        assert result.vat_amount == Decimal("18.03")

    def test_eu_cross_border_b2b_fr_to_de_charges_sellers_rate(self) -> None:
        """Cross-border B2B from France to Germany is taxed at the French seller's rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("120.00"),
            seller_vat_rate=Decimal("20.00"),
            seller_country="FR",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )

        assert result.reverse_charge is False
        assert result.effective_price == Decimal("120.00")
        assert result.vat_amount == Decimal("20.00")  # 120 incl. 20% -> net 100 + VAT 20


class TestDetermineAttendeeVatEUCrossBorderB2C:
    """EU cross-border B2C: different EU country, no VAT ID -> full VAT."""

    def test_eu_cross_border_b2c_charges_seller_vat(self) -> None:
        """Buyer in different EU country without VAT ID pays seller's VAT rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=False,
        )

        assert result.effective_price == Decimal("122.00")
        assert result.vat_rate == Decimal("22.00")
        assert result.reverse_charge is False

    def test_eu_cross_border_b2c_same_as_domestic_b2c(self) -> None:
        """EU B2C cross-border and domestic B2C yield the same effective price."""
        gross = Decimal("100.00")
        vat_rate = Decimal("22.00")

        domestic = determine_attendee_vat(
            gross_price=gross,
            seller_vat_rate=vat_rate,
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=False,
        )
        cross_border = determine_attendee_vat(
            gross_price=gross,
            seller_vat_rate=vat_rate,
            seller_country="IT",
            buyer_country="FR",
            buyer_vat_id_valid=False,
        )

        assert domestic.effective_price == cross_border.effective_price
        assert domestic.vat_amount == cross_border.vat_amount


class TestDetermineAttendeeVatNonEU:
    """Non-EU buyer: full VAT at the seller's rate — no export zero-rating (#868)."""

    def test_non_eu_buyer_pays_full_vat(self) -> None:
        """Buyer outside the EU pays the gross price at the seller's rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="US",
            buyer_vat_id_valid=False,
        )

        assert result.effective_price == Decimal("122.00")
        assert result.vat_rate == Decimal("22.00")
        assert result.vat_amount == Decimal("22.00")
        assert result.reverse_charge is False

    def test_non_eu_buyer_with_vat_id_still_pays_full_vat(self) -> None:
        """Non-EU buyer with a (foreign) VAT ID also pays full VAT."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="US",
            buyer_vat_id_valid=True,
        )

        assert result.effective_price == Decimal("100.00")
        assert result.net_amount == Decimal("81.97")
        assert result.vat_amount == Decimal("18.03")

    @pytest.mark.parametrize("country", ["US", "GB", "CH", "NO", "JP", "AU", "CA"])
    def test_non_eu_countries_all_pay_full_vat(self, country: str) -> None:
        """Various non-EU countries all pay gross at the seller's rate."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country=country,
            buyer_vat_id_valid=False,
        )

        assert result.effective_price == Decimal("100.00")
        assert result.vat_amount == Decimal("18.03")
        assert result.reverse_charge is False


class TestDetermineAttendeeVatEdgeCases:
    """Edge cases and buyer-profile invariance."""

    def test_country_casing_never_affects_the_result(self) -> None:
        """Countries no longer affect the price, so casing trivially cannot either."""
        result_lower = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="it",
            buyer_country="de",
            buyer_vat_id_valid=True,
        )
        result_upper = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )

        assert result_lower == result_upper

    @pytest.mark.parametrize(
        ("buyer_country", "buyer_vat_id_valid"),
        [
            ("IT", False),  # domestic B2C
            ("IT", True),  # domestic B2B
            ("DE", True),  # EU cross-border B2B (formerly reverse charge)
            ("DE", False),  # EU cross-border B2C
            ("US", False),  # non-EU (formerly export zero-rated)
            ("US", True),  # non-EU with foreign VAT ID
        ],
        ids=["domestic-b2c", "domestic-b2b", "eu-b2b", "eu-b2c", "non-eu", "non-eu-b2b"],
    )
    def test_buyer_profile_never_affects_the_result(self, buyer_country: str, buyer_vat_id_valid: bool) -> None:
        """Same gross and rate yield the IDENTICAL result for every buyer profile (#868)."""
        baseline = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=False,
        )
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country=buyer_country,
            buyer_vat_id_valid=buyer_vat_id_valid,
        )

        assert result == baseline

    def test_zero_vat_rate_yields_no_vat(self) -> None:
        """A 0% VAT rate should result in zero VAT regardless of scenario."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("0.00"),
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=False,
        )

        assert result.vat_amount == Decimal("0.00")
        assert result.net_amount == Decimal("100.00")
        assert result.effective_price == Decimal("100.00")

    def test_result_is_frozen_dataclass(self) -> None:
        """AttendeeVATResult should be immutable (frozen dataclass)."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=False,
        )

        with pytest.raises(AttributeError):
            result.vat_rate = Decimal("0.00")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_effective_vat_rate — tier override vs org fallback
# ---------------------------------------------------------------------------


class TestGetEffectiveVatRate:
    """Test tier-level VAT rate override vs organization default."""

    def test_tier_override_takes_precedence(self) -> None:
        """When tier.vat_rate is set, it should override the org default."""
        tier = MagicMock()
        tier.vat_rate = Decimal("10.00")
        org = MagicMock()
        org.vat_rate = Decimal("22.00")

        assert get_effective_vat_rate(tier, org) == Decimal("10.00")

    def test_org_fallback_when_tier_rate_is_none(self) -> None:
        """When tier.vat_rate is None, the org's VAT rate should be used."""
        tier = MagicMock()
        tier.vat_rate = None
        org = MagicMock()
        org.vat_rate = Decimal("22.00")

        assert get_effective_vat_rate(tier, org) == Decimal("22.00")

    def test_tier_zero_rate_is_valid_override(self) -> None:
        """A tier with vat_rate=0 is a valid override (not a fallback to org)."""
        tier = MagicMock()
        tier.vat_rate = Decimal("0.00")
        org = MagicMock()
        org.vat_rate = Decimal("22.00")

        assert get_effective_vat_rate(tier, org) == Decimal("0.00")
