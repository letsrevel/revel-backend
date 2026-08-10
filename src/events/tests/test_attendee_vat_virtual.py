"""Unit tests for the virtual-event branches of ``determine_attendee_vat`` (#869).

Physical admission (``is_virtual=False``, the default) is taxed where the event
takes place (#868): every buyer pays the gross price at the seller's rate, and
the buyer's country / VAT-ID status never changes the outcome. Virtual
attendance applies the general place-of-supply rules: cross-border EU B2B with
a validated VAT ID is reverse-charged (buyer pays net); cross-border EU B2C
pays the seller's rate (interim OSS treatment); a non-EU buyer owes no EU VAT.

Pure arithmetic over a frozen dataclass — no DB.
"""

from decimal import Decimal

import pytest

from events.service.attendee_vat_service import AttendeeVATResult, determine_attendee_vat

GROSS = Decimal("122.00")
RATE = Decimal("22.00")
NET = Decimal("100.00")
VAT = Decimal("22.00")

FULL_VAT = AttendeeVATResult(
    effective_price=GROSS,
    net_amount=NET,
    vat_amount=VAT,
    vat_rate=RATE,
    reverse_charge=False,
)


class TestVirtualDomestic:
    """Same-country virtual attendance is a plain domestic supply."""

    @pytest.mark.parametrize("vat_id_valid", [True, False])
    def test_domestic_buyer_pays_full_vat(self, vat_id_valid: bool) -> None:
        """A domestic buyer pays gross at the seller's rate, VAT ID or not."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="IT",
            buyer_vat_id_valid=vat_id_valid,
            is_virtual=True,
        )
        assert result == FULL_VAT

    @pytest.mark.parametrize(
        "seller,buyer",
        [("EL", "GR"), ("GR", "EL"), ("EL", "EL"), ("GR", "GR")],
    )
    def test_greece_el_gr_treated_as_same_country(self, seller: str, buyer: str) -> None:
        """EL (VIES prefix) and GR (ISO) normalize to one country — never cross-border."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country=seller,
            buyer_country=buyer,
            buyer_vat_id_valid=True,  # even a validated VAT ID must not reverse-charge
            is_virtual=True,
        )
        assert result == FULL_VAT

    def test_lowercase_country_codes_are_normalized(self) -> None:
        """Lowercase codes compare equal to their uppercase forms."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="it",
            buyer_country="It",
            buyer_vat_id_valid=True,
            is_virtual=True,
        )
        assert result == FULL_VAT


class TestVirtualCrossBorderEU:
    """Cross-border EU buyers of virtual attendance."""

    def test_b2b_validated_vat_id_is_reverse_charged(self) -> None:
        """EU B2B with a validated VAT ID pays net; VAT shifts to the buyer."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
            is_virtual=True,
        )
        assert result == AttendeeVATResult(
            effective_price=NET,
            net_amount=NET,
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            reverse_charge=True,
        )

    def test_b2c_pays_full_gross_at_seller_rate(self) -> None:
        """EU B2C (no validated VAT ID) pays gross — the interim OSS treatment."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=False,
            is_virtual=True,
        )
        assert result == FULL_VAT

    def test_el_prefixed_buyer_cross_border_b2b_reverse_charged(self) -> None:
        """A Greek buyer (EL prefix) of a non-Greek seller is genuinely cross-border."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="EL",
            buyer_vat_id_valid=True,
            is_virtual=True,
        )
        assert result.reverse_charge is True
        assert result.effective_price == NET


class TestVirtualNonEU:
    """Non-EU buyers of virtual attendance are outside EU VAT scope."""

    @pytest.mark.parametrize("vat_id_valid", [True, False])
    def test_non_eu_buyer_pays_net_without_vat(self, vat_id_valid: bool) -> None:
        """A non-EU buyer pays net with no VAT and no reverse charge, VAT ID or not."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="US",
            buyer_vat_id_valid=vat_id_valid,
            is_virtual=True,
        )
        assert result == AttendeeVATResult(
            effective_price=NET,
            net_amount=NET,
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            reverse_charge=False,
        )


class TestPhysicalDefault:
    """Physical admission (is_virtual=False) never varies by buyer."""

    @pytest.mark.parametrize(
        "buyer_country,vat_id_valid",
        [
            ("IT", False),  # domestic B2C
            ("IT", True),  # domestic B2B
            ("DE", True),  # EU cross-border B2B — no reverse charge for admission
            ("DE", False),  # EU cross-border B2C
            ("GR", True),  # EL/GR normalization irrelevant on the physical path
            ("US", True),  # non-EU — no export zero-rating for admission
            ("US", False),
        ],
    )
    def test_every_buyer_pays_gross_at_seller_rate(self, buyer_country: str, vat_id_valid: bool) -> None:
        """Identical full-VAT result regardless of buyer country and VAT-ID status (#868)."""
        result = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country=buyer_country,
            buyer_vat_id_valid=vat_id_valid,
        )
        assert result == FULL_VAT

    def test_is_virtual_defaults_to_false(self) -> None:
        """Omitting is_virtual gives the physical (full-VAT) treatment."""
        explicit = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
            is_virtual=False,
        )
        omitted = determine_attendee_vat(
            gross_price=GROSS,
            seller_vat_rate=RATE,
            seller_country="IT",
            buyer_country="DE",
            buyer_vat_id_valid=True,
        )
        assert explicit == omitted == FULL_VAT
