"""Tests for attendee VAT calculation logic.

Tests cover:
- determine_attendee_vat() for all 5 EU VAT scenarios:
  - Domestic B2C (same country, no VAT ID) -> full VAT
  - Domestic B2B (same country, valid VAT ID) -> full VAT
  - EU cross-border B2B (different EU country, valid VAT ID) -> reverse charge
  - EU cross-border B2C (different EU country, no VAT ID) -> full VAT
  - Non-EU buyer -> no VAT (export)
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
# determine_attendee_vat — 5 EU VAT scenarios
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
    """EU cross-border B2B: different EU country, valid VAT ID -> reverse charge."""

    def test_eu_cross_border_b2b_reverse_charge(self) -> None:
        """Buyer in a different EU country with valid VAT ID gets reverse charge (0% VAT)."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )

        assert result.reverse_charge is True
        assert result.vat_amount == Decimal("0.00")
        assert result.vat_rate == Decimal("0.00")
        # Buyer pays net amount only
        assert result.effective_price == result.net_amount

    def test_eu_cross_border_b2b_effective_price_is_net(self) -> None:
        """Reverse charge means buyer pays the net amount, not the gross."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="FR",
            buyer_vat_id_valid=True,
        )

        # Net = 100 / 1.22 = 81.97
        assert result.effective_price == Decimal("81.97")
        assert result.net_amount == Decimal("81.97")

    def test_eu_cross_border_b2b_fr_to_de(self) -> None:
        """Cross-border B2B from France to Germany triggers reverse charge."""
        result = determine_attendee_vat(
            gross_price=Decimal("120.00"),
            seller_vat_rate=Decimal("20.00"),
            seller_country="FR",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )

        assert result.reverse_charge is True
        assert result.vat_amount == Decimal("0.00")
        assert result.effective_price == Decimal("100.00")  # 120 / 1.20


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
    """Non-EU buyer: no VAT (export of services)."""

    def test_non_eu_buyer_no_vat(self) -> None:
        """Buyer outside the EU pays no VAT (export of services)."""
        result = determine_attendee_vat(
            gross_price=Decimal("122.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="US",
            buyer_vat_id_valid=False,
        )

        assert result.vat_amount == Decimal("0.00")
        assert result.vat_rate == Decimal("0.00")
        assert result.reverse_charge is False
        # Buyer pays net amount only
        assert result.effective_price == result.net_amount

    def test_non_eu_buyer_with_vat_id_still_no_vat(self) -> None:
        """Non-EU buyer with a (foreign) VAT ID also pays no VAT."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country="US",
            buyer_vat_id_valid=True,
        )

        assert result.vat_amount == Decimal("0.00")
        assert result.effective_price == Decimal("81.97")

    @pytest.mark.parametrize("country", ["US", "GB", "CH", "NO", "JP", "AU", "CA"])
    def test_non_eu_countries_are_vat_exempt(self, country: str) -> None:
        """Various non-EU countries should all be VAT-exempt."""
        result = determine_attendee_vat(
            gross_price=Decimal("100.00"),
            seller_vat_rate=Decimal("22.00"),
            seller_country="IT",
            buyer_country=country,
            buyer_vat_id_valid=False,
        )

        assert result.vat_amount == Decimal("0.00")
        assert result.reverse_charge is False


class TestDetermineAttendeeVatEdgeCases:
    """Edge cases and normalization."""

    def test_country_codes_are_case_insensitive(self) -> None:
        """Lowercase country codes should be treated identically to uppercase."""
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
