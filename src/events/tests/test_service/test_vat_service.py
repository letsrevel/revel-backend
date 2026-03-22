"""Tests for the VAT calculation service.

Covers:
- VAT-inclusive price breakdown (calculate_vat_inclusive)
- Platform fee VAT with EU reverse charge rules (calculate_platform_fee_vat)
- Effective VAT rate resolution (get_effective_vat_rate)
- Amount distribution across items (distribute_amount_across_items)
- EU_MEMBER_STATES constant integrity
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from common.constants import EU_MEMBER_STATES
from events.service.vat_service import (
    PlatformFeeVATBreakdown,
    VATBreakdown,
    calculate_platform_fee_vat,
    calculate_vat_inclusive,
    distribute_amount_across_items,
    get_effective_vat_rate,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# EU_MEMBER_STATES constant
# ---------------------------------------------------------------------------


class TestEUMemberStates:
    """Verify the EU member states constant is accurate."""

    def test_contains_28_entries(self) -> None:
        """27 EU members + EL (Greece VIES prefix alongside ISO GR)."""
        assert len(EU_MEMBER_STATES) == 28

    @pytest.mark.parametrize(
        "country_code",
        [
            "AT",
            "BE",
            "BG",
            "CY",
            "CZ",
            "DE",
            "DK",
            "EE",
            "EL",
            "ES",
            "FI",
            "FR",
            "GR",
            "HR",
            "HU",
            "IE",
            "IT",
            "LT",
            "LU",
            "LV",
            "MT",
            "NL",
            "PL",
            "PT",
            "RO",
            "SE",
            "SI",
            "SK",
        ],
    )
    def test_contains_expected_country(self, country_code: str) -> None:
        """All 27 EU member states (plus EL for Greece) should be present."""
        assert country_code in EU_MEMBER_STATES

    def test_does_not_contain_gb_post_brexit(self) -> None:
        """GB left the EU; it must not appear in the set."""
        assert "GB" not in EU_MEMBER_STATES

    @pytest.mark.parametrize("country_code", ["US", "CH", "NO", "JP", "CN", "AU"])
    def test_does_not_contain_non_eu_countries(self, country_code: str) -> None:
        """Non-EU countries must not be in the set."""
        assert country_code not in EU_MEMBER_STATES

    def test_is_frozenset(self) -> None:
        """The constant should be immutable."""
        assert isinstance(EU_MEMBER_STATES, frozenset)


# ---------------------------------------------------------------------------
# calculate_vat_inclusive
# ---------------------------------------------------------------------------


class TestCalculateVatInclusive:
    """Test VAT-inclusive price breakdown calculation."""

    def test_standard_italian_vat_22_percent(self) -> None:
        """Standard Italian VAT (22%) applied to a EUR 100.00 gross amount.

        100.00 / 1.22 = 81.97 (rounded), VAT = 18.03.
        """
        result = calculate_vat_inclusive(Decimal("100.00"), Decimal("22.00"))

        assert result.gross_amount == Decimal("100.00")
        assert result.net_amount == Decimal("81.97")
        assert result.vat_amount == Decimal("18.03")
        assert result.vat_rate == Decimal("22.00")

    def test_german_vat_19_percent(self) -> None:
        """German standard VAT (19%) on EUR 119.00.

        119.00 / 1.19 = 100.00 exact, VAT = 19.00.
        """
        result = calculate_vat_inclusive(Decimal("119.00"), Decimal("19.00"))

        assert result.net_amount == Decimal("100.00")
        assert result.vat_amount == Decimal("19.00")

    @pytest.mark.parametrize(
        ("gross", "vat_rate", "expected_net", "expected_vat"),
        [
            # 10% reduced rate
            (Decimal("110.00"), Decimal("10.00"), Decimal("100.00"), Decimal("10.00")),
            # 5% super-reduced rate
            (Decimal("105.00"), Decimal("5.00"), Decimal("100.00"), Decimal("5.00")),
            # 22% standard (IT)
            (Decimal("122.00"), Decimal("22.00"), Decimal("100.00"), Decimal("22.00")),
        ],
        ids=["10%", "5%", "22%"],
    )
    def test_standard_vat_rates_on_round_net(
        self,
        gross: Decimal,
        vat_rate: Decimal,
        expected_net: Decimal,
        expected_vat: Decimal,
    ) -> None:
        """When gross is exactly (net * (1 + rate)), breakdown should be exact."""
        result = calculate_vat_inclusive(gross, vat_rate)

        assert result.net_amount == expected_net
        assert result.vat_amount == expected_vat

    def test_vat_rate_zero_returns_no_vat(self) -> None:
        """A zero VAT rate means no VAT: net equals gross."""
        result = calculate_vat_inclusive(Decimal("50.00"), Decimal("0"))

        assert result.gross_amount == Decimal("50.00")
        assert result.net_amount == Decimal("50.00")
        assert result.vat_amount == Decimal("0.00")
        assert result.vat_rate == Decimal("0.00")

    def test_negative_vat_rate_treated_as_zero(self) -> None:
        """Negative VAT rates are nonsensical and treated as zero."""
        result = calculate_vat_inclusive(Decimal("75.00"), Decimal("-5.00"))

        assert result.net_amount == Decimal("75.00")
        assert result.vat_amount == Decimal("0.00")
        assert result.vat_rate == Decimal("0.00")

    def test_accounting_identity_gross_equals_net_plus_vat(self) -> None:
        """Critical accounting invariant: gross = net + vat must always hold."""
        result = calculate_vat_inclusive(Decimal("99.99"), Decimal("22.00"))

        assert result.gross_amount == result.net_amount + result.vat_amount

    @pytest.mark.parametrize(
        ("gross", "vat_rate"),
        [
            (Decimal("0.01"), Decimal("22.00")),
            (Decimal("0.05"), Decimal("22.00")),
            (Decimal("0.10"), Decimal("19.00")),
            (Decimal("0.01"), Decimal("10.00")),
        ],
        ids=["1-cent-22%", "5-cent-22%", "10-cent-19%", "1-cent-10%"],
    )
    def test_small_amounts_maintain_accounting_identity(self, gross: Decimal, vat_rate: Decimal) -> None:
        """Even for very small amounts, gross = net + vat must hold."""
        result = calculate_vat_inclusive(gross, vat_rate)

        assert result.gross_amount == result.net_amount + result.vat_amount
        assert result.net_amount >= Decimal("0")
        assert result.vat_amount >= Decimal("0")

    def test_large_amount(self) -> None:
        """VAT calculation works correctly for large ticket prices."""
        result = calculate_vat_inclusive(Decimal("10000.00"), Decimal("22.00"))

        # 10000 / 1.22 = 8196.72 (rounded)
        assert result.net_amount == Decimal("8196.72")
        assert result.vat_amount == Decimal("1803.28")
        assert result.gross_amount == result.net_amount + result.vat_amount

    def test_rounding_half_up_behavior(self) -> None:
        """Verify ROUND_HALF_UP is used, not bankers' rounding.

        10.05 / 1.22 = 8.237704918... which rounds to 8.24 with HALF_UP.
        """
        result = calculate_vat_inclusive(Decimal("10.05"), Decimal("22.00"))

        assert result.gross_amount == result.net_amount + result.vat_amount
        # net = 10.05 / 1.22 = 8.2377... -> 8.24 with ROUND_HALF_UP
        assert result.net_amount == Decimal("8.24")
        assert result.vat_amount == Decimal("1.81")

    def test_returns_vat_breakdown_dataclass(self) -> None:
        """Return type is VATBreakdown frozen dataclass."""
        result = calculate_vat_inclusive(Decimal("100.00"), Decimal("22.00"))

        assert isinstance(result, VATBreakdown)

    @pytest.mark.parametrize(
        ("gross", "vat_rate"),
        [
            (Decimal("33.33"), Decimal("22.00")),
            (Decimal("77.77"), Decimal("19.00")),
            (Decimal("0.99"), Decimal("10.00")),
            (Decimal("1234.56"), Decimal("5.00")),
        ],
        ids=["33.33-22%", "77.77-19%", "0.99-10%", "1234.56-5%"],
    )
    def test_arbitrary_amounts_maintain_accounting_identity(self, gross: Decimal, vat_rate: Decimal) -> None:
        """Fuzz-style test: accounting identity holds for various inputs."""
        result = calculate_vat_inclusive(gross, vat_rate)

        assert result.gross_amount == result.net_amount + result.vat_amount


# ---------------------------------------------------------------------------
# calculate_platform_fee_vat
# ---------------------------------------------------------------------------


def _make_org_mock(
    vat_country_code: str = "",
    vat_id: str = "",
    vat_id_validated: bool = False,
) -> Mock:
    """Create a mock Organization with VAT-related fields.

    Args:
        vat_country_code: ISO 3166-1 alpha-2 country code.
        vat_id: VAT identification number.
        vat_id_validated: Whether the VAT ID has been validated via VIES.

    Returns:
        A Mock configured with the given VAT attributes.
    """
    org = Mock(spec=["vat_country_code", "vat_id", "vat_id_validated"])
    org.vat_country_code = vat_country_code
    org.vat_id = vat_id
    org.vat_id_validated = vat_id_validated
    return org


class TestCalculatePlatformFeeVat:
    """Test platform fee VAT with EU reverse charge rules."""

    PLATFORM_COUNTRY = "IT"
    PLATFORM_VAT_RATE = Decimal("22.00")
    FEE = Decimal("10.00")

    def test_outside_eu_org_no_vat(self) -> None:
        """US org (outside EU): export of services, no VAT charged.

        Fee is treated as net; reverse_charge is False (not applicable).
        """
        org = _make_org_mock(vat_country_code="US")

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == self.FEE
        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.fee_vat_rate == Decimal("0.00")
        assert result.reverse_charge is False

    def test_gb_post_brexit_treated_as_non_eu(self) -> None:
        """GB is no longer in the EU; treated as export of services."""
        org = _make_org_mock(vat_country_code="GB", vat_id="GB123456789", vat_id_validated=True)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.reverse_charge is False

    def test_eu_cross_border_with_valid_vat_id_reverse_charge(self) -> None:
        """German org with validated VAT ID: B2B reverse charge applies.

        Fee is treated as net; org self-assesses VAT in their country.
        """
        org = _make_org_mock(vat_country_code="DE", vat_id="DE123456789", vat_id_validated=True)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == self.FEE
        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.fee_vat_rate == Decimal("0.00")
        assert result.reverse_charge is True

    def test_eu_cross_border_without_valid_vat_id_domestic_vat(self) -> None:
        """German org WITHOUT validated VAT ID: platform's domestic VAT applies.

        Cannot use reverse charge, so the platform extracts Italian VAT.
        """
        org = _make_org_mock(vat_country_code="DE", vat_id="DE123456789", vat_id_validated=False)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        # 10.00 / 1.22 = 8.20 (rounded), VAT = 1.80
        assert result.fee_net == Decimal("8.20")
        assert result.fee_vat == Decimal("1.80")
        assert result.fee_vat_rate == self.PLATFORM_VAT_RATE
        assert result.reverse_charge is False

    def test_eu_cross_border_no_vat_id_at_all(self) -> None:
        """French org with no VAT ID: domestic VAT applies."""
        org = _make_org_mock(vat_country_code="FR", vat_id="", vat_id_validated=False)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.20")
        assert result.fee_vat == Decimal("1.80")
        assert result.reverse_charge is False

    def test_same_country_with_valid_vat_id_domestic_vat(self) -> None:
        """Italian org with valid VAT ID: same-country, domestic VAT applies.

        Reverse charge only applies cross-border, not domestically.
        """
        org = _make_org_mock(vat_country_code="IT", vat_id="IT12345678901", vat_id_validated=True)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.20")
        assert result.fee_vat == Decimal("1.80")
        assert result.fee_vat_rate == self.PLATFORM_VAT_RATE
        assert result.reverse_charge is False

    def test_same_country_without_vat_id_domestic_vat(self) -> None:
        """Italian org without VAT ID: domestic VAT applies regardless."""
        org = _make_org_mock(vat_country_code="IT", vat_id="", vat_id_validated=False)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.20")
        assert result.fee_vat == Decimal("1.80")
        assert result.reverse_charge is False

    def test_empty_vat_country_code_treated_as_non_eu(self) -> None:
        """Org with no country code set: treated as outside EU (no VAT)."""
        org = _make_org_mock(vat_country_code="", vat_id="", vat_id_validated=False)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.reverse_charge is False

    def test_lowercase_country_codes_work(self) -> None:
        """Country codes should be case-insensitive (org side)."""
        org = _make_org_mock(vat_country_code="de", vat_id="DE123456789", vat_id_validated=True)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        # Should be detected as EU cross-border with valid VAT -> reverse charge
        assert result.reverse_charge is True
        assert result.fee_vat == Decimal("0.00")

    def test_lowercase_org_same_country(self) -> None:
        """Lowercase org country code should match platform country correctly."""
        org = _make_org_mock(vat_country_code="it", vat_id="IT12345678901", vat_id_validated=True)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        # Same country -> domestic VAT, not reverse charge
        assert result.reverse_charge is False
        assert result.fee_vat == Decimal("1.80")

    def test_accounting_identity_fee_gross_equals_net_plus_vat(self) -> None:
        """For domestic VAT cases, fee_gross = fee_net + fee_vat must hold."""
        org = _make_org_mock(vat_country_code="IT")

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == result.fee_net + result.fee_vat

    def test_returns_platform_fee_vat_breakdown_dataclass(self) -> None:
        """Return type is PlatformFeeVATBreakdown frozen dataclass."""
        org = _make_org_mock(vat_country_code="US")

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert isinstance(result, PlatformFeeVATBreakdown)

    @pytest.mark.parametrize(
        ("org_country", "vat_id", "validated", "expected_rc", "expected_vat_zero"),
        [
            # All EU countries cross-border with valid VAT -> reverse charge
            ("DE", "DE123", True, True, True),
            ("FR", "FR123", True, True, True),
            ("ES", "ES123", True, True, True),
            ("NL", "NL123", True, True, True),
            # EU cross-border without valid VAT -> domestic VAT
            ("DE", "DE123", False, False, False),
            ("FR", "", False, False, False),
            # Same country -> domestic VAT
            ("IT", "IT123", True, False, False),
            ("IT", "", False, False, False),
            # Non-EU -> no VAT, no reverse charge
            ("US", "", False, False, True),
            ("CH", "CHE123", True, False, True),
        ],
        ids=[
            "DE-valid-vat",
            "FR-valid-vat",
            "ES-valid-vat",
            "NL-valid-vat",
            "DE-invalid-vat",
            "FR-no-vat",
            "IT-same-valid",
            "IT-same-no-vat",
            "US-non-eu",
            "CH-non-eu",
        ],
    )
    def test_parametrized_vat_scenarios(
        self,
        org_country: str,
        vat_id: str,
        validated: bool,
        expected_rc: bool,
        expected_vat_zero: bool,
    ) -> None:
        """Table-driven test covering key VAT scenario combinations."""
        org = _make_org_mock(vat_country_code=org_country, vat_id=vat_id, vat_id_validated=validated)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.reverse_charge is expected_rc
        if expected_vat_zero:
            assert result.fee_vat == Decimal("0.00")
        else:
            assert result.fee_vat > Decimal("0.00")

    def test_vat_id_present_but_not_validated_no_reverse_charge(self) -> None:
        """A VAT ID that has not been validated via VIES does not qualify for reverse charge."""
        org = _make_org_mock(vat_country_code="NL", vat_id="NL123456789B01", vat_id_validated=False)

        result = calculate_platform_fee_vat(self.FEE, org, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.reverse_charge is False
        assert result.fee_vat > Decimal("0.00")


# ---------------------------------------------------------------------------
# get_effective_vat_rate
# ---------------------------------------------------------------------------


class TestGetEffectiveVatRate:
    """Test VAT rate resolution with tier override and org fallback."""

    def test_tier_override_takes_precedence(self) -> None:
        """When the tier has a specific VAT rate, it overrides the org default."""
        result = get_effective_vat_rate(tier_vat_rate=Decimal("10.00"), org_vat_rate=Decimal("22.00"))

        assert result == Decimal("10.00")

    def test_falls_back_to_org_rate_when_tier_is_none(self) -> None:
        """When tier VAT rate is None, the org's default rate is used."""
        result = get_effective_vat_rate(tier_vat_rate=None, org_vat_rate=Decimal("22.00"))

        assert result == Decimal("22.00")

    def test_tier_zero_is_valid_override(self) -> None:
        """A tier rate of 0 is a valid override (tax-exempt tier), not a fallback.

        Zero is not None, so it should NOT fall back to the org rate.
        """
        result = get_effective_vat_rate(tier_vat_rate=Decimal("0"), org_vat_rate=Decimal("22.00"))

        assert result == Decimal("0")

    def test_both_none_tier_and_zero_org(self) -> None:
        """When tier is None and org rate is 0, the effective rate is 0."""
        result = get_effective_vat_rate(tier_vat_rate=None, org_vat_rate=Decimal("0"))

        assert result == Decimal("0")

    def test_returns_decimal(self) -> None:
        """Return value should always be a Decimal."""
        result = get_effective_vat_rate(tier_vat_rate=None, org_vat_rate=Decimal("19.00"))

        assert isinstance(result, Decimal)


# ---------------------------------------------------------------------------
# distribute_amount_across_items
# ---------------------------------------------------------------------------


class TestDistributeAmountAcrossItems:
    """Test amount distribution ensuring penny-perfect accounting."""

    def test_equal_distribution(self) -> None:
        """EUR 9.00 / 3 items = exactly 3.00 each, no remainder."""
        result = distribute_amount_across_items(Decimal("9.00"), 3)

        assert result == [Decimal("3.00"), Decimal("3.00"), Decimal("3.00")]

    def test_remainder_pennies_added_to_first_items(self) -> None:
        """EUR 10.00 / 3 items: base 3.33, first item gets extra penny.

        10.00 / 3 = 3.33 (ROUND_HALF_UP), 3 * 3.33 = 9.99, remainder = 0.01.
        First item is adjusted to 3.34.
        """
        result = distribute_amount_across_items(Decimal("10.00"), 3)

        assert result[0] == Decimal("3.34")
        assert result[1] == Decimal("3.33")
        assert result[2] == Decimal("3.33")

    def test_single_item_returns_total(self) -> None:
        """A single item gets the entire amount."""
        result = distribute_amount_across_items(Decimal("42.99"), 1)

        assert result == [Decimal("42.99")]

    def test_count_zero_returns_empty_list(self) -> None:
        """Zero items means nothing to distribute."""
        result = distribute_amount_across_items(Decimal("10.00"), 0)

        assert result == []

    def test_negative_count_returns_empty_list(self) -> None:
        """Negative count is treated the same as zero."""
        result = distribute_amount_across_items(Decimal("10.00"), -1)

        assert result == []

    def test_sum_always_equals_total(self) -> None:
        """Critical accounting invariant: sum of distributed amounts = total."""
        total = Decimal("10.00")

        result = distribute_amount_across_items(total, 3)

        assert sum(result) == total

    @pytest.mark.parametrize(
        ("total", "count"),
        [
            (Decimal("0.01"), 3),
            (Decimal("0.10"), 3),
            (Decimal("1.00"), 7),
            (Decimal("100.00"), 3),
            (Decimal("100.00"), 7),
            (Decimal("999.99"), 11),
            (Decimal("0.03"), 2),
            (Decimal("10000.00"), 97),
        ],
        ids=[
            "1-cent-3-items",
            "10-cents-3-items",
            "1-euro-7-items",
            "100-euro-3-items",
            "100-euro-7-items",
            "999.99-11-items",
            "3-cents-2-items",
            "10k-97-items",
        ],
    )
    def test_sum_invariant_parametrized(self, total: Decimal, count: int) -> None:
        """Sum of results always equals the input total for various inputs."""
        result = distribute_amount_across_items(total, count)

        assert len(result) == count
        assert sum(result) == total

    def test_all_items_non_negative(self) -> None:
        """No item should receive a negative amount."""
        result = distribute_amount_across_items(Decimal("0.01"), 5)

        assert all(amount >= Decimal("0") for amount in result)
        assert sum(result) == Decimal("0.01")

    def test_two_items_odd_cent(self) -> None:
        """EUR 0.03 / 2: base 0.02 (ROUND_HALF_UP), remainder -0.01.

        3 / 2 = 1.5 cents -> 0.02 with ROUND_HALF_UP. 2 * 0.02 = 0.04, off by -0.01.
        First item adjusted down: [0.01, 0.02].
        """
        result = distribute_amount_across_items(Decimal("0.03"), 2)

        assert sum(result) == Decimal("0.03")
        assert len(result) == 2

    def test_large_count_sum_invariant(self) -> None:
        """Even with many items, the sum must exactly equal the total."""
        total = Decimal("1000.00")
        count = 300

        result = distribute_amount_across_items(total, count)

        assert len(result) == count
        assert sum(result) == total

    def test_result_list_length_matches_count(self) -> None:
        """The returned list should have exactly count elements."""
        result = distribute_amount_across_items(Decimal("50.00"), 13)

        assert len(result) == 13

    def test_zero_total_distributed_evenly(self) -> None:
        """Distributing zero across items gives zero to each."""
        result = distribute_amount_across_items(Decimal("0.00"), 5)

        assert result == [Decimal("0.00")] * 5
        assert sum(result) == Decimal("0.00")

    def test_items_differ_by_at_most_one_penny(self) -> None:
        """All items should be within 0.01 of each other (fair distribution)."""
        result = distribute_amount_across_items(Decimal("100.00"), 7)

        min_val = min(result)
        max_val = max(result)
        assert max_val - min_val <= Decimal("0.01")
