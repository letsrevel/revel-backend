"""Structured billing address fields shared by billable models."""

from django.db import models

from common.postal_address import PostalAddress


class BillingPostalAddressMixin(models.Model):
    """Mixin adding a structured billing address (``billing_*`` columns).

    All fields are optional (``blank=True, default=""``) so existing flows keep working
    without a billing address. Use :meth:`billing_postal_address` to get the value type.
    """

    billing_address_line1 = models.CharField(max_length=255, blank=True, default="", db_default="")
    billing_address_line2 = models.CharField(max_length=255, blank=True, default="", db_default="")
    billing_postal_code = models.CharField(max_length=20, blank=True, default="", db_default="")
    billing_city = models.CharField(max_length=100, blank=True, default="", db_default="")
    billing_subdivision = models.CharField(max_length=100, blank=True, default="", db_default="")
    billing_country_code = models.CharField(max_length=2, blank=True, default="", db_default="")

    class Meta:
        abstract = True

    def billing_postal_address(self) -> PostalAddress:
        """Return the billing address as a :class:`~common.postal_address.PostalAddress`."""
        return PostalAddress.from_fields(self, "billing_")
