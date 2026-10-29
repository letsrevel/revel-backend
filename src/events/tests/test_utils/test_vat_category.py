"""Tests for the EN 16931 VAT category derivation and exemption map."""

import typing as t
from decimal import Decimal

from events.utils.vat_category import VatCategory, build_vat_exemptions, derive_vat_category


def test_reverse_charge_wins_over_rate() -> None:
    assert derive_vat_category(Decimal("20"), True) is VatCategory.REVERSE_CHARGE


def test_positive_rate_is_standard() -> None:
    assert derive_vat_category(Decimal("19"), False) is VatCategory.STANDARD


def test_zero_rate_is_exempt() -> None:
    assert derive_vat_category(Decimal("0.00"), False) is VatCategory.EXEMPT


def test_exemptions_only_for_non_standard() -> None:
    assert build_vat_exemptions([VatCategory.STANDARD, VatCategory.STANDARD], []) == {}


def test_reverse_charge_exemption() -> None:
    assert build_vat_exemptions([VatCategory.REVERSE_CHARGE], []) == {
        "AE": {"text": "Reverse charge", "code": "VATEX-EU-AE"}
    }


def test_exempt_reasons_joined_distinct_in_order() -> None:
    result = build_vat_exemptions(
        [VatCategory.EXEMPT, VatCategory.EXEMPT, VatCategory.STANDARD],
        ["§4 Nr. 20 UStG", "", "§4 Nr. 20 UStG", "§6 Abs. 1 Z 26 UStG"],
    )
    assert result == {"E": {"text": "§4 Nr. 20 UStG; §6 Abs. 1 Z 26 UStG", "code": ""}}


def test_exempt_without_reason_falls_back() -> None:
    assert build_vat_exemptions([VatCategory.EXEMPT], []) == {"E": {"text": "Exempt from VAT", "code": ""}}


def test_exempt_reasons_are_stripped() -> None:
    result: dict[str, t.Any] = build_vat_exemptions([VatCategory.EXEMPT], ["  §4 Nr. 20 UStG  ", "   "])
    assert result["E"]["text"] == "§4 Nr. 20 UStG"
