"""``python manage.py scrub_stripe_products [--dry-run]``.

One-off backfill for #848: outbound Stripe payloads no longer carry
organizer-authored content, but Products already persisted in Stripe catalogs
still do. This command scrubs them in place:

1. **Plan products** — every ``MembershipSubscriptionPlan`` with a
   ``stripe_product_id`` is renamed to the generic ``"Membership"`` and its
   description emptied.
2. **Catalog sweep** — for each distinct connected account (platform account
   included once, with no ``stripe_account`` header), the ad-hoc Products that
   Stripe materialized from past inline ``price_data`` are renamed:
   ``"Ticket: …"`` → ``"Ticket"``, ``"Season pass: …"`` → ``"Season pass"``.

Idempotent: already-generic names don't match a prefix and are skipped; plan
products are re-modified unconditionally (a server-side no-op). Per-account
Stripe errors are logged and skipped so one dead account can't abort the sweep.
"""

import typing as t

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from events.models import MembershipSubscriptionPlan, Organization
from events.service.subscription_stripe_payloads import _stripe_account_kwargs

stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION

# Prefixes of the legacy organizer-authored product names → generic replacement.
_PREFIX_TO_GENERIC: dict[str, str] = {
    "Ticket: ": "Ticket",
    "Season pass: ": "Season pass",
}


class Command(BaseCommand):
    """Scrub organizer-authored names/descriptions out of existing Stripe Products (#848)."""

    help = (
        "Rename organizer-authored Stripe Products to generic labels: plan products "
        "become 'Membership' (description cleared), and catalog products created from "
        "past inline price_data lose their 'Ticket: …' / 'Season pass: …' names."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire the ``--dry-run`` flag."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log every would-be modification without calling Stripe's modify API.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Scrub plan products, then sweep every Stripe account's product catalog."""
        dry_run: bool = options["dry_run"]
        plans_scrubbed = products_renamed = accounts_swept = errors = 0

        plans = MembershipSubscriptionPlan.objects.exclude(stripe_product_id="").select_related("tier__organization")
        for plan in plans:
            account_kwargs = _stripe_account_kwargs(plan.tier.organization)
            if dry_run:
                self.stdout.write(f"[dry-run] would scrub plan product {plan.stripe_product_id} (plan {plan.pk})")
                plans_scrubbed += 1
                continue
            try:
                stripe.Product.modify(plan.stripe_product_id, name="Membership", description="", **account_kwargs)
            except stripe.error.StripeError as exc:
                self.stderr.write(f"Failed to scrub plan product {plan.stripe_product_id}: {exc}")
                errors += 1
                continue
            plans_scrubbed += 1

        account_ids = (
            Organization.objects.exclude(stripe_account_id=None)
            .exclude(stripe_account_id="")
            .values_list("stripe_account_id", flat=True)
            .distinct()
        )
        for account_id in (t.cast(str, aid) for aid in account_ids):  # exclude() above rules out None
            # An org sharing the platform's own account is swept without the
            # Connect header; ``distinct()`` guarantees it appears only once.
            account_kwargs = {} if account_id == settings.STRIPE_ACCOUNT else {"stripe_account": account_id}
            try:
                for product in stripe.Product.list(limit=100, **account_kwargs).auto_paging_iter():
                    generic = next(
                        (new for prefix, new in _PREFIX_TO_GENERIC.items() if product["name"].startswith(prefix)),
                        None,
                    )
                    if generic is None:
                        continue
                    if dry_run:
                        self.stdout.write(f"[dry-run] would rename product {product['id']} to {generic!r}")
                        products_renamed += 1
                        continue
                    try:
                        stripe.Product.modify(product["id"], name=generic, **account_kwargs)
                    except stripe.error.StripeError as exc:
                        self.stderr.write(f"Failed to rename product {product['id']} on {account_id}: {exc}")
                        errors += 1
                        continue
                    products_renamed += 1
            except stripe.error.StripeError as exc:
                self.stderr.write(f"Failed to sweep account {account_id}: {exc}")
                errors += 1
                continue
            accounts_swept += 1

        self.stdout.write(
            f"Done: {plans_scrubbed} plan product(s) scrubbed, {products_renamed} product(s) renamed, "
            f"{accounts_swept} account(s) swept, {errors} error(s)."
        )
