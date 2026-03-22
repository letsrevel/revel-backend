# src/common/tests/test_exchange_rate_service.py
"""Tests for the exchange rate service."""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from common.models import ExchangeRate
from common.service.exchange_rate_service import convert, fetch_and_store_rates, get_latest_rates, get_rate

pytestmark = pytest.mark.django_db

SAMPLE_RATES = {
    "USD": 1.08,
    "GBP": 0.86,
    "JPY": 162.5,
    "CHF": 0.97,
}


@pytest.fixture
def exchange_rate() -> ExchangeRate:
    return ExchangeRate.objects.create(
        base="EUR",
        date=datetime.date(2026, 3, 20),
        rates=SAMPLE_RATES,
    )


@pytest.fixture
def older_exchange_rate() -> ExchangeRate:
    return ExchangeRate.objects.create(
        base="EUR",
        date=datetime.date(2026, 3, 19),
        rates={"USD": 1.07, "GBP": 0.85, "JPY": 161.0, "CHF": 0.96},
    )


def test_get_latest_rates(exchange_rate: ExchangeRate) -> None:
    """Test fetching the latest exchange rate record."""
    result = get_latest_rates("EUR")
    assert result.id == exchange_rate.id
    assert result.date == datetime.date(2026, 3, 20)


def test_get_latest_rates_returns_most_recent(exchange_rate: ExchangeRate, older_exchange_rate: ExchangeRate) -> None:
    """Test that latest() returns the most recent date."""
    result = get_latest_rates("EUR")
    assert result.date == datetime.date(2026, 3, 20)


def test_get_rate_same_currency() -> None:
    """Test that same-currency conversion returns 1."""
    assert get_rate("EUR", "EUR") == Decimal("1")


def test_get_rate_from_base(exchange_rate: ExchangeRate) -> None:
    """Test rate from base currency to target."""
    rate = get_rate("EUR", "USD")
    assert rate == Decimal("1.08")


def test_get_rate_to_base(exchange_rate: ExchangeRate) -> None:
    """Test rate from target currency to base."""
    rate = get_rate("USD", "EUR")
    assert rate == Decimal("1") / Decimal("1.08")


def test_get_rate_cross(exchange_rate: ExchangeRate) -> None:
    """Test cross rate between two non-base currencies."""
    rate = get_rate("USD", "GBP")
    expected = Decimal("0.86") / Decimal("1.08")
    assert rate == expected


def test_get_rate_for_specific_date(exchange_rate: ExchangeRate, older_exchange_rate: ExchangeRate) -> None:
    """Test that a specific date uses the nearest available rate on or before."""
    rate = get_rate("EUR", "USD", date=datetime.date(2026, 3, 19))
    assert rate == Decimal("1.07")


def test_convert_same_currency() -> None:
    """Test that converting to the same currency is a no-op."""
    result = convert(Decimal("100.00"), "EUR", "EUR")
    assert result == Decimal("100.00")


def test_convert_to_different_currency(exchange_rate: ExchangeRate) -> None:
    """Test converting EUR to USD."""
    result = convert(Decimal("100.00"), "EUR", "USD")
    assert result == Decimal("108.00")


def test_convert_from_non_base(exchange_rate: ExchangeRate) -> None:
    """Test converting USD to EUR."""
    result = convert(Decimal("108.00"), "USD", "EUR")
    assert result == Decimal("100.00")


@patch("common.service.exchange_rate_service.httpx.get")
def test_fetch_and_store_rates(mock_get: MagicMock) -> None:
    """Test fetching rates from frankfurter.app and storing them."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "amount": 1.0,
        "base": "EUR",
        "date": "2026-03-21",
        "rates": SAMPLE_RATES,
    }
    mock_response.raise_for_status = MagicMock()
    mock_get.return_value = mock_response

    result = fetch_and_store_rates("EUR")

    assert result.base == "EUR"
    assert result.date == datetime.date(2026, 3, 21)
    assert result.rates == SAMPLE_RATES
    assert ExchangeRate.objects.count() == 1


@patch("common.service.exchange_rate_service.httpx.get")
def test_fetch_and_store_rates_idempotent(mock_get: MagicMock) -> None:
    """Test that re-fetching for the same date updates instead of creating."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "amount": 1.0,
        "base": "EUR",
        "date": "2026-03-21",
        "rates": SAMPLE_RATES,
    }
    mock_response.raise_for_status = MagicMock()
    mock_get.return_value = mock_response

    fetch_and_store_rates("EUR")
    fetch_and_store_rates("EUR")

    assert ExchangeRate.objects.count() == 1


def test_get_rate_no_rates_raises() -> None:
    """Test that get_rate raises when no rates exist."""
    with pytest.raises(ExchangeRate.DoesNotExist):
        get_rate("EUR", "USD")
