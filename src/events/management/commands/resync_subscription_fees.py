"""Backfill/repair ``application_fee_percent`` on live Stripe subscriptions.

The percent is frozen into each Stripe Subscription at Checkout; VAT-status
changes now resync it automatically (``events.resync_org_subscription_fees``),
but this command covers everything the automatic path can't: pre-existing
drift, orgs whose fee settings were changed by admins, resync-task failures,
and schedule-managed rows once their schedule has released.
"""

import time
import typing as t

from django.core.management.base import BaseCommand, CommandError

from events.models import MembershipSubscription, MembershipSubscriptionPlan, Organization
from events.service.subscription_stripe_service import (
    effective_application_fee_percent,
    resync_subscription_application_fees,
)


class Command(BaseCommand):
    """Resync the effective platform fee percent onto every live ONLINE subscription."""

    help = "Push each org's current effective application_fee_percent onto its live Stripe subscriptions"

    def add_arguments(self, parser: t.Any) -> None:
        """Add command arguments.

        Args:
            parser: The argument parser.
        """
        parser.add_argument(
            "--org-slug",
            action="append",
            default=None,
            help="Limit to this organization slug (repeatable). Default: all orgs with live ONLINE subscriptions.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.2,
            help="Seconds to sleep between Stripe calls on the same Connect account (default: 0.2).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be pushed without calling Stripe.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the resync.

        Args:
            args: Positional arguments.
            options: Keyword arguments from command line.

        Raises:
            CommandError: If an --org-slug does not exist, or (after a full run)
                if any subscription failed to resync.
        """
        eligible = (
            MembershipSubscription.objects.filter(
                plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            )
            .exclude(stripe_subscription_id="")
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        )
        orgs = Organization.objects.filter(pk__in=eligible.values("organization_id")).order_by("slug")
        if options["org_slug"]:
            orgs = orgs.filter(slug__in=options["org_slug"])
            missing = set(options["org_slug"]) - set(orgs.values_list("slug", flat=True))
            if missing:
                raise CommandError(f"Unknown or subscription-less org slug(s): {', '.join(sorted(missing))}")

        total_updated = total_skipped = total_failed = 0
        for org in orgs:
            target = effective_application_fee_percent(org)
            target_label = f"{target}%" if target is not None else "no fee (clear)"
            if options["dry_run"]:
                org_subs = eligible.filter(organization=org)
                schedule_managed = org_subs.exclude(stripe_schedule_id="").count()
                to_update = org_subs.count() - schedule_managed
                total_updated += to_update
                total_skipped += schedule_managed
                line = f"[dry-run] {org.slug}: would push {target_label} to {to_update} subscription(s)"
                if schedule_managed:
                    line += f", skipping {schedule_managed} schedule-managed"
                self.stdout.write(line)
                continue

            counters = resync_subscription_application_fees(org, sleep_seconds=options["sleep"])
            total_updated += counters["updated"]
            total_skipped += counters["skipped_schedule_managed"]
            total_failed += counters["failed"]
            style = self.style.ERROR if counters["failed"] else self.style.SUCCESS
            self.stdout.write(
                style(
                    f"{org.slug}: pushed {target_label} — {counters['updated']} updated, "
                    f"{counters['skipped_schedule_managed']} schedule-managed skipped, "
                    f"{counters['failed']} failed"
                )
            )
            # The per-call sleep rate-limits within one Connect account; a beat
            # between orgs keeps the platform-account request rate polite too.
            if options["sleep"]:
                time.sleep(options["sleep"])

        summary = f"Done: {total_updated} updated, {total_skipped} schedule-managed skipped, {total_failed} failed."
        if total_skipped:
            summary += " Re-run once pending downgrades release their schedules."
        self.stdout.write(summary)
        if total_failed:
            raise CommandError(
                f"{total_failed} subscription(s) failed to resync — see logs (subscription_fee_resync_failed)."
            )
