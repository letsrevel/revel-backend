"""Tests for the PostalAddress value type (EN 16931 structured addresses, #1234)."""

import dataclasses
import types

import pytest

from common.postal_address import PostalAddress


def test_missing_fields_lists_required_parts_in_order() -> None:
    assert PostalAddress(city="Wien").missing_fields() == ["line1", "postal_code", "country_code"]


def test_complete_address() -> None:
    address = PostalAddress(line1="Musterstraße 42", postal_code="1010", city="Wien", country_code="AT")

    assert address.missing_fields() == []
    assert address.is_complete


def test_subdivision_and_line2_are_not_required() -> None:
    address = PostalAddress(line1="Musterstraße 42", postal_code="1010", city="Wien", country_code="AT")

    assert address.is_complete
    assert address.subdivision == ""
    assert address.line2 == ""


def test_display_lines_skips_blanks() -> None:
    address = PostalAddress(line1="Musterstraße 42", postal_code="1010", city="Wien", country_code="AT")

    assert address.display_lines() == ["Musterstraße 42", "1010 Wien", "AT"]


def test_display_lines_includes_line2_and_subdivision() -> None:
    address = PostalAddress(
        line1="Main St 1",
        line2="Suite 4",
        postal_code="94105",
        city="San Francisco",
        subdivision="CA",
        country_code="US",
    )

    assert address.display_lines() == ["Main St 1", "Suite 4", "94105 San Francisco", "CA", "US"]


def test_display_lines_without_postal_code_does_not_leave_stray_space() -> None:
    assert PostalAddress(city="Wien").display_lines() == ["Wien"]


def test_empty_address() -> None:
    address = PostalAddress()

    assert address.is_empty
    assert address.display_lines() == []
    assert address.missing_fields() == ["line1", "postal_code", "city", "country_code"]


def test_non_empty_address_is_not_empty() -> None:
    assert not PostalAddress(city="Wien").is_empty


def test_is_frozen() -> None:
    address = PostalAddress(city="Wien")

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(address, "city", "Graz")  # noqa: B010


@pytest.mark.parametrize("prefix", ["billing_", "seller_", "buyer_"])
def test_to_fields_from_fields_round_trip(prefix: str) -> None:
    """to_fields -> object -> from_fields returns the same address, with prefixed names."""
    address = PostalAddress("L1", "L2", "1010", "Wien", "W", "AT")
    fields = address.to_fields(prefix)
    assert set(fields) == {f"{prefix}{s}" for s in PostalAddress.FIELD_SUFFIXES.values()}
    assert PostalAddress.from_fields(types.SimpleNamespace(**fields), prefix) == address


def test_to_fields_names() -> None:
    """The billing prefix yields the Organization/profile column names."""
    assert list(PostalAddress().to_fields("billing_")) == [
        "billing_address_line1",
        "billing_address_line2",
        "billing_postal_code",
        "billing_city",
        "billing_subdivision",
        "billing_country_code",
    ]
