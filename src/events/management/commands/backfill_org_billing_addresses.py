"""One-off backfill of structured org billing addresses from Stripe Connect (#1234).

Prints ids and outcomes only, never names, emails, addresses or Stripe ids.
"""

import typing as t
from collections import Counter

import stripe
import structlog
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandParser

from common.country import normalize_country_code
from common.service.stripe_config import configure_stripe
from events.models import Organization
from events.service.stripe_address_service import apply_postal_address, postal_address_from_stripe

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Fill empty structured billing addresses of Stripe-connected organizations."""

    help = "Pre-fill structured billing addresses of connected organizations from Stripe."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register ``--dry-run``."""
        parser.add_argument("--dry-run", action="store_true", help="Report outcomes without saving.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Process every connected organization that has no structured line 1 (partial manual entries are skipped)."""
        configure_stripe()
        dry_run: bool = options["dry_run"]
        counts: Counter[str] = Counter()
        qs = Organization.objects.filter(billing_address_line1="").exclude(stripe_account_id__isnull=True)
        # Materialise: the loop saves rows and PgBouncer breaks server-side cursors (engineering-notes).
        for org in list(qs.exclude(stripe_account_id="")):
            outcome = self._process(org, dry_run)
            counts[outcome] += 1
            self.stdout.write(f"{org.id} {outcome}")
        self.stdout.write(
            " ".join(f"{k}={counts[k]}" for k in ("filled", "partial", "country_only", "skipped", "error"))
        )

    def _process(self, org: Organization, dry_run: bool) -> str:
        if not org.billing_postal_address().is_empty:  # hand-entered data wins
            return "skipped"
        try:
            account = stripe.Account.retrieve(str(org.stripe_account_id))
        except stripe.StripeError:
            logger.warning("backfill_org_billing_address_stripe_error", org_id=str(org.id))
            return "error"
        fields = apply_postal_address(org, postal_address_from_stripe(account))
        outcome = "filled" if org.billing_address_line1 else "partial"
        if not fields:
            if not (org.vat_country_code and not org.billing_country_code):
                return "skipped"
            org.billing_country_code = normalize_country_code(org.vat_country_code)
            fields = ["billing_country_code"]
            outcome = "country_only"
        if not dry_run:
            try:
                org.save(update_fields=[*fields, "updated_at"])
            except ValidationError:
                logger.warning("backfill_org_billing_address_validation_error", org_id=str(org.id))
                return "error"
        return outcome
