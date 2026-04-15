"""Exchange rate service using frankfurter.dev (ECB data).

Provides daily exchange rates with automatic fetching and DB caching.
Rates are fetched by a daily Celery task and stored in the ExchangeRate model.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx
import structlog
from django.conf import settings

from common.models import ExchangeRate

logger = structlog.get_logger(__name__)

FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"


def fetch_and_store_rates(base: str | None = None, date: datetime.date | None = None) -> ExchangeRate:
    """Fetch exchange rates from frankfurter.app and store in the database.

    Args:
        base: Base currency (defaults to DEFAULT_CURRENCY).
        date: Date to fetch rates for (defaults to latest).

    Returns:
        The created or updated ExchangeRate record.
    """
    base = base or settings.DEFAULT_CURRENCY
    url = f"{FRANKFURTER_BASE_URL}/latest" if date is None else f"{FRANKFURTER_BASE_URL}/{date.isoformat()}"

    response = httpx.get(url, params={"base": base}, timeout=15)
    response.raise_for_status()
    data = response.json()

    rate_date = datetime.date.fromisoformat(data["date"])

    exchange_rate, created = ExchangeRate.objects.update_or_create(
        base=base,
        date=rate_date,
        defaults={"rates": data["rates"]},
    )

    logger.info(
        "exchange_rates_stored",
        action="fetched" if created else "updated",
        base=base,
        date=str(rate_date),
        currencies=len(data["rates"]),
    )
    return exchange_rate


def get_latest_rates(base: str | None = None) -> ExchangeRate:
    """Get the latest exchange rates from the database.

    Args:
        base: Base currency (defaults to DEFAULT_CURRENCY).

    Returns:
        The latest ExchangeRate record.

    Raises:
        ExchangeRate.DoesNotExist: If no rates are stored yet.
    """
    base = base or settings.DEFAULT_CURRENCY
    return ExchangeRate.objects.filter(base=base).latest("date")


def get_rate(from_currency: str, to_currency: str, date: datetime.date | None = None) -> Decimal:
    """Get the exchange rate between two currencies.

    Args:
        from_currency: Source currency code.
        to_currency: Target currency code.
        date: Specific date (defaults to latest available).

    Returns:
        The exchange rate as a Decimal.

    Raises:
        ExchangeRate.DoesNotExist: If no rates are stored.
        KeyError: If a currency is not found in the rates.
    """
    if from_currency == to_currency:
        return Decimal("1")

    base = settings.DEFAULT_CURRENCY

    if date:
        exchange_rate = ExchangeRate.objects.filter(base=base, date__lte=date).latest("date")
    else:
        exchange_rate = ExchangeRate.objects.filter(base=base).latest("date")

    rates = exchange_rate.rates

    if from_currency == base:
        return Decimal(str(rates[to_currency]))
    elif to_currency == base:
        return Decimal("1") / Decimal(str(rates[from_currency]))
    else:
        # Cross rate: from_currency → base → to_currency
        from_rate = Decimal(str(rates[from_currency]))
        to_rate = Decimal(str(rates[to_currency]))
        return to_rate / from_rate


def convert(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    date: datetime.date | None = None,
) -> Decimal:
    """Convert an amount between currencies.

    Args:
        amount: The amount to convert.
        from_currency: Source currency code.
        to_currency: Target currency code.
        date: Specific date for the rate (defaults to latest available).

    Returns:
        The converted amount, rounded to 2 decimal places.
    """
    if from_currency == to_currency:
        return amount

    rate = get_rate(from_currency, to_currency, date)
    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def convert_using_rates(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    rates: dict[str, float],
    base: str | None = None,
) -> Decimal:
    """Convert an amount using a pre-fetched rates dict (no DB query).

    Useful in loops where the same exchange rates are reused for many conversions.

    Args:
        amount: The amount to convert.
        from_currency: Source currency code.
        to_currency: Target currency code.
        rates: Mapping of currency code → rate relative to base.
        base: The base currency of the rates dict (defaults to DEFAULT_CURRENCY).

    Returns:
        The converted amount, rounded to 2 decimal places.
    """
    if from_currency == to_currency:
        return amount

    base = base or settings.DEFAULT_CURRENCY

    if from_currency == base:
        rate = Decimal(str(rates[to_currency]))
    elif to_currency == base:
        rate = Decimal("1") / Decimal(str(rates[from_currency]))
    else:
        rate = Decimal(str(rates[to_currency])) / Decimal(str(rates[from_currency]))

    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
