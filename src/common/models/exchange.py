"""Currency exchange-rate model."""

from django.conf import settings
from django.db import models

from common.models.base import TimeStampedModel


class ExchangeRate(TimeStampedModel):
    """Daily exchange rates from frankfurter.app (ECB data).

    Stores rates in the frankfurter response format: base currency with a JSON
    object mapping target currencies to their rates.
    """

    base = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    date = models.DateField(db_index=True)
    rates = models.JSONField(help_text="Mapping of currency code → rate relative to base")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["base", "date"], name="unique_exchange_rate_per_day")]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.base} rates for {self.date} ({len(self.rates)} currencies)"
