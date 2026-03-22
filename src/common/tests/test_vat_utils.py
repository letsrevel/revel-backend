"""Tests for common.service.vat_utils -- shared B2B VAT determination logic.

Covers:
- ``calculate_vat_inclusive``: VAT-inclusive breakdown with positive, zero,
  and negative rates.
- ``calculate_b2b_fee_vat``: All four EU VAT scenarios (same country,
  EU cross-border with valid VAT, EU without valid VAT, outside EU).
- ``VATEntity`` protocol compatibility with a plain dataclass.
"""

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import Mock

import pytest

from common.service.vat_utils import (
    B2BFeeVATBreakdown,
    VATBreakdown,
    calculate_b2b_fee_vat,
    calculate_vat_inclusive,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# VATEntity protocol
# ---------------------------------------------------------------------------


@dataclass
class _FakeVATEntity:
    """Minimal dataclass satisfying the VATEntity protocol for testing."""

    vat_country_code: str = ""
    vat_id: str = ""
    vat_id_validated: bool = False


def _make_entity(
    vat_country_code: str = "",
    vat_id: str = "",
    vat_id_validated: bool = False,
) -> _FakeVATEntity:
    """Create a fake VAT entity with the given attributes.

    Args:
        vat_country_code: ISO 3166-1 alpha-2 country code.
        vat_id: VAT identification number.
        vat_id_validated: Whether the VAT ID was validated via VIES.

    Returns:
        A ``_FakeVATEntity`` configured with the given attributes.
    """
    return _FakeVATEntity(
        vat_country_code=vat_country_code,
        vat_id=vat_id,
        vat_id_validated=vat_id_validated,
    )


class TestVATEntityProtocol:
    """Verify the VATEntity protocol works with different implementations."""

    def test_dataclass_satisfies_protocol(self) -> None:
        """A plain dataclass with the right fields satisfies VATEntity at runtime."""
        entity = _FakeVATEntity(vat_country_code="DE", vat_id="DE123", vat_id_validated=True)

        # The entity should work as an argument to calculate_b2b_fee_vat
        result = calculate_b2b_fee_vat(
            fee=Decimal("10.00"),
            entity=entity,
            platform_vat_country="AT",
            platform_vat_rate=Decimal("20.00"),
        )

        assert isinstance(result, B2BFeeVATBreakdown)

    def test_mock_satisfies_protocol(self) -> None:
        """A Mock with the right spec attributes also works as VATEntity."""
        mock_entity = Mock(spec=["vat_country_code", "vat_id", "vat_id_validated"])
        mock_entity.vat_country_code = "US"
        mock_entity.vat_id = ""
        mock_entity.vat_id_validated = False

        result = calculate_b2b_fee_vat(
            fee=Decimal("5.00"),
            entity=mock_entity,
            platform_vat_country="AT",
            platform_vat_rate=Decimal("20.00"),
        )

        assert isinstance(result, B2BFeeVATBreakdown)


# ---------------------------------------------------------------------------
# calculate_vat_inclusive
# ---------------------------------------------------------------------------


class TestCalculateVatInclusive:
    """Test VAT-inclusive price breakdown calculation."""

    def test_austrian_vat_20_percent(self) -> None:
        """Standard Austrian VAT (20%) on EUR 120.00.

        120.00 / 1.20 = 100.00 exact, VAT = 20.00.
        """
        result = calculate_vat_inclusive(Decimal("120.00"), Decimal("20.00"))

        assert result.gross_amount == Decimal("120.00")
        assert result.net_amount == Decimal("100.00")
        assert result.vat_amount == Decimal("20.00")
        assert result.vat_rate == Decimal("20.00")

    def test_italian_vat_22_percent(self) -> None:
        """Italian standard VAT (22%) on EUR 100.00.

        100.00 / 1.22 = 81.97 (rounded HALF_UP), VAT = 18.03.
        """
        result = calculate_vat_inclusive(Decimal("100.00"), Decimal("22.00"))

        assert result.net_amount == Decimal("81.97")
        assert result.vat_amount == Decimal("18.03")
        assert result.vat_rate == Decimal("22.00")

    def test_zero_vat_rate_returns_no_vat(self) -> None:
        """A zero VAT rate means net equals gross, no VAT extracted."""
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
        """Critical invariant: gross = net + vat must always hold."""
        result = calculate_vat_inclusive(Decimal("99.99"), Decimal("20.00"))

        assert result.gross_amount == result.net_amount + result.vat_amount

    def test_returns_vat_breakdown_dataclass(self) -> None:
        """Return type is the VATBreakdown frozen dataclass."""
        result = calculate_vat_inclusive(Decimal("100.00"), Decimal("20.00"))

        assert isinstance(result, VATBreakdown)

    @pytest.mark.parametrize(
        ("gross", "vat_rate"),
        [
            (Decimal("0.01"), Decimal("20.00")),
            (Decimal("33.33"), Decimal("20.00")),
            (Decimal("77.77"), Decimal("19.00")),
            (Decimal("1234.56"), Decimal("5.00")),
        ],
        ids=["1-cent-20%", "33.33-20%", "77.77-19%", "1234.56-5%"],
    )
    def test_accounting_identity_parametrized(self, gross: Decimal, vat_rate: Decimal) -> None:
        """Accounting identity holds for various input combinations."""
        result = calculate_vat_inclusive(gross, vat_rate)

        assert result.gross_amount == result.net_amount + result.vat_amount


# ---------------------------------------------------------------------------
# calculate_b2b_fee_vat
# ---------------------------------------------------------------------------


class TestCalculateB2BFeeVat:
    """Test B2B fee VAT determination with EU reverse charge rules.

    The platform is registered in Austria (AT) with a 20% domestic VAT rate.
    """

    PLATFORM_COUNTRY = "AT"
    PLATFORM_VAT_RATE = Decimal("20.00")
    FEE = Decimal("10.00")

    # --- Scenario 1: Same country ---

    def test_same_country_with_valid_vat_id_domestic_vat(self) -> None:
        """Austrian entity with valid VAT ID: same-country, domestic VAT applies.

        Reverse charge only applies cross-border, not domestically.
        10.00 / 1.20 = 8.33, VAT = 1.67.
        """
        entity = _make_entity(vat_country_code="AT", vat_id="ATU12345678", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.33")
        assert result.fee_vat == Decimal("1.67")
        assert result.fee_vat_rate == self.PLATFORM_VAT_RATE
        assert result.reverse_charge is False

    def test_same_country_without_vat_id_domestic_vat(self) -> None:
        """Austrian entity without VAT ID: domestic VAT applies regardless."""
        entity = _make_entity(vat_country_code="AT", vat_id="", vat_id_validated=False)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.33")
        assert result.fee_vat == Decimal("1.67")
        assert result.reverse_charge is False

    # --- Scenario 2: EU cross-border with valid VAT ID ---

    def test_eu_cross_border_with_valid_vat_id_reverse_charge(self) -> None:
        """German entity with validated VAT ID: B2B reverse charge applies.

        Fee is treated as net; entity self-assesses VAT in their country.
        """
        entity = _make_entity(vat_country_code="DE", vat_id="DE123456789", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == self.FEE
        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.fee_vat_rate == Decimal("0.00")
        assert result.reverse_charge is True

    # --- Scenario 3: EU without valid VAT ID ---

    def test_eu_cross_border_without_valid_vat_id_domestic_vat(self) -> None:
        """German entity WITHOUT validated VAT ID: platform's domestic VAT applies.

        Cannot use reverse charge, so the platform extracts Austrian VAT.
        """
        entity = _make_entity(vat_country_code="DE", vat_id="DE123456789", vat_id_validated=False)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.33")
        assert result.fee_vat == Decimal("1.67")
        assert result.fee_vat_rate == self.PLATFORM_VAT_RATE
        assert result.reverse_charge is False

    def test_eu_cross_border_no_vat_id_at_all_domestic_vat(self) -> None:
        """French entity with no VAT ID: domestic VAT applies."""
        entity = _make_entity(vat_country_code="FR", vat_id="", vat_id_validated=False)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == Decimal("8.33")
        assert result.fee_vat == Decimal("1.67")
        assert result.reverse_charge is False

    # --- Scenario 4: Outside EU ---

    def test_outside_eu_no_vat(self) -> None:
        """US entity (outside EU): export of services, no VAT charged.

        Fee is treated as net; reverse_charge is False (not applicable).
        """
        entity = _make_entity(vat_country_code="US")

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == self.FEE
        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.fee_vat_rate == Decimal("0.00")
        assert result.reverse_charge is False

    def test_gb_post_brexit_treated_as_non_eu(self) -> None:
        """GB is no longer in the EU; treated as export of services."""
        entity = _make_entity(vat_country_code="GB", vat_id="GB123456789", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.reverse_charge is False

    def test_switzerland_non_eu(self) -> None:
        """Swiss entity: export of services, no VAT."""
        entity = _make_entity(vat_country_code="CH", vat_id="CHE123456789", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.reverse_charge is False

    # --- Edge cases ---

    def test_empty_vat_country_code_treated_as_non_eu(self) -> None:
        """Entity with no country code set: treated as outside EU (no VAT)."""
        entity = _make_entity(vat_country_code="", vat_id="", vat_id_validated=False)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_net == self.FEE
        assert result.fee_vat == Decimal("0.00")
        assert result.reverse_charge is False

    def test_lowercase_country_codes_work(self) -> None:
        """Country codes should be case-insensitive (entity side)."""
        entity = _make_entity(vat_country_code="de", vat_id="DE123456789", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        # Should be detected as EU cross-border with valid VAT -> reverse charge
        assert result.reverse_charge is True
        assert result.fee_vat == Decimal("0.00")

    def test_lowercase_entity_same_country(self) -> None:
        """Lowercase entity country code should match platform country correctly."""
        entity = _make_entity(vat_country_code="at", vat_id="ATU12345678", vat_id_validated=True)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        # Same country -> domestic VAT, not reverse charge
        assert result.reverse_charge is False
        assert result.fee_vat == Decimal("1.67")

    def test_vat_id_present_but_not_validated_no_reverse_charge(self) -> None:
        """A VAT ID that has not been validated via VIES does not qualify for reverse charge."""
        entity = _make_entity(vat_country_code="NL", vat_id="NL123456789B01", vat_id_validated=False)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.reverse_charge is False
        assert result.fee_vat > Decimal("0.00")

    def test_accounting_identity_fee_gross_equals_net_plus_vat(self) -> None:
        """For domestic VAT cases, fee_gross = fee_net + fee_vat must hold."""
        entity = _make_entity(vat_country_code="AT")

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.fee_gross == result.fee_net + result.fee_vat

    def test_returns_b2b_fee_vat_breakdown_dataclass(self) -> None:
        """Return type is B2BFeeVATBreakdown frozen dataclass."""
        entity = _make_entity(vat_country_code="US")

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert isinstance(result, B2BFeeVATBreakdown)

    @pytest.mark.parametrize(
        ("entity_country", "vat_id", "validated", "expected_rc", "expected_vat_zero"),
        [
            # EU cross-border with valid VAT -> reverse charge
            ("DE", "DE123", True, True, True),
            ("FR", "FR123", True, True, True),
            ("IT", "IT123", True, True, True),
            ("NL", "NL123", True, True, True),
            # EU cross-border without valid VAT -> domestic VAT
            ("DE", "DE123", False, False, False),
            ("FR", "", False, False, False),
            # Same country -> domestic VAT
            ("AT", "ATU123", True, False, False),
            ("AT", "", False, False, False),
            # Non-EU -> no VAT, no reverse charge
            ("US", "", False, False, True),
            ("CH", "CHE123", True, False, True),
            ("GB", "GB123", True, False, True),
        ],
        ids=[
            "DE-valid-vat",
            "FR-valid-vat",
            "IT-valid-vat",
            "NL-valid-vat",
            "DE-invalid-vat",
            "FR-no-vat",
            "AT-same-valid",
            "AT-same-no-vat",
            "US-non-eu",
            "CH-non-eu",
            "GB-post-brexit",
        ],
    )
    def test_parametrized_vat_scenarios(
        self,
        entity_country: str,
        vat_id: str,
        validated: bool,
        expected_rc: bool,
        expected_vat_zero: bool,
    ) -> None:
        """Table-driven test covering key VAT scenario combinations."""
        entity = _make_entity(vat_country_code=entity_country, vat_id=vat_id, vat_id_validated=validated)

        result = calculate_b2b_fee_vat(self.FEE, entity, self.PLATFORM_COUNTRY, self.PLATFORM_VAT_RATE)

        assert result.reverse_charge is expected_rc
        if expected_vat_zero:
            assert result.fee_vat == Decimal("0.00")
        else:
            assert result.fee_vat > Decimal("0.00")
