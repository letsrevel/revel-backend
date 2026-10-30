"""``python manage.py provision_stripe_webhooks --url <https://...>``.

Creates the two Stripe webhook endpoints Revel needs (platform + Connect),
both pinned to ``settings.STRIPE_API_VERSION``, and prints the resulting
``whsec_*`` signing secrets so the operator can paste them into
``STRIPE_WEBHOOK_SECRETS`` (CSV) on the deploy.

Both endpoints target the SAME URL. Stripe distinguishes them by the
``connect`` flag, so a single ``/api/stripe/webhook`` URL serves both
"Your account" platform deliveries and "Connected accounts" deliveries,
each with its own signing secret. ``verify_webhook`` tries each secret in
turn (see ``events/service/stripe_webhooks.py``).

The command refuses to run if any endpoint already targets the URL, unless
``--force`` is given (cutover: the old endpoint stays active alongside the
new ones until the operator disables it in the dashboard — the event-log
dedup makes the overlap harmless).

``--sync`` is the maintenance mode: it makes the already-provisioned pair
(exactly one platform and one Connect endpoint at the URL) match the
``PLATFORM_EVENTS`` / ``CONNECT_EVENTS`` lists below, so a release that adds
event types needs ``--sync --dry-run`` then ``--sync`` instead of editing the
endpoints by hand. Signing secrets and the API version are untouched.
"""

import json
import typing as t

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from common.service.stripe_config import configure_stripe

# Must stay in sync with the dispatch map in
# events/service/stripe_webhooks.py::StripeEventHandler.handle — Stripe only
# delivers events you subscribe to.
PLATFORM_EVENTS: tuple[str, ...] = (
    "checkout.session.completed",
    # Abandoned membership checkouts: frees the PENDING row's cap slot.
    "checkout.session.expired",
    "charge.refunded",
    # Card disputes: recorded next to the payment, organizers notified (#1245).
    "charge.dispute.created",
    "charge.dispute.updated",
    "charge.dispute.closed",
    "payment_intent.canceled",
    # Membership subscriptions live on connected accounts (direct charges,
    # ADR-0012), but a host-as-org binding (#495) routes the same events
    # through the platform endpoint — subscribe both.
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    # Off-session renewals blocked on SCA/3DS: the invoice stays open with no
    # immediate payment_failed, so this is the only prompt signal.
    "invoice.payment_action_required",
    # A released schedule clears the local pending-downgrade fields. Needed
    # because the release is irreversible on Stripe while the local clear can
    # still be rolled back (cancel/pause raising after it), and because an org
    # can release a schedule straight from the Stripe Dashboard.
    "subscription_schedule.released",
)
CONNECT_EVENTS: tuple[str, ...] = PLATFORM_EVENTS + ("account.updated",)


class Command(BaseCommand):
    """Provision the platform + Connect webhook endpoints in Stripe."""

    help = (
        "Create two Stripe webhook endpoints (platform + Connect) at the given "
        "URL and print the whsec_* signing secrets for STRIPE_WEBHOOK_SECRETS."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire ``--url`` (required), ``--dry-run`` and ``--force`` flags."""
        parser.add_argument(
            "--url",
            required=True,
            help="Public URL of /api/stripe/webhook (e.g. https://api.letsrevel.io/api/stripe/webhook).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without calling the Stripe API.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Create even if endpoints already target this URL (cutover overlap).",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help=(
                "Update the existing platform + Connect endpoints at --url so their enabled events match "
                "the canonical lists (adds missing, removes stale). Secrets are unchanged and the API "
                "version is NOT changed (it is set only at creation). Combine with --dry-run to preview. "
                "Mutually exclusive with --force."
            ),
        )
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format. 'json' prints only a machine-readable object (for setup.sh).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Validate ``--url``, then either preview or POST two endpoints to Stripe."""
        url: str = options["url"]
        if not url.startswith("https://"):
            raise CommandError(f"--url must be https:// (Stripe rejects http in live mode). Got: {url}")

        if options["sync"]:
            if options["force"]:
                raise CommandError("--sync and --force are mutually exclusive.")
            self._sync(url, dry_run=options["dry_run"], as_json=options["format"] == "json")
            return

        if options["dry_run"] and options["format"] == "json":
            # --format json promises JSON-only output; --dry-run emits human text, so refuse the combo.
            raise CommandError("--dry-run cannot be combined with --format json.")

        if options["dry_run"]:
            self.stdout.write("DRY RUN — no API calls.")
            self.stdout.write(f"Would create platform endpoint at {url}: {', '.join(PLATFORM_EVENTS)}")
            self.stdout.write(f"Would create Connect endpoint at {url}: {', '.join(CONNECT_EVENTS)}")
            return

        configure_stripe()

        existing = [ep for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter() if ep.url == url]
        if existing and not options["force"]:
            ids = ", ".join(ep.id for ep in existing)
            raise CommandError(
                f"Endpoint(s) already target {url}: {ids}. Re-run with --force to create "
                "the new pair alongside them (then disable the old one in the dashboard)."
            )

        platform = stripe.WebhookEndpoint.create(
            url=url,
            enabled_events=t.cast(t.Any, list(PLATFORM_EVENTS)),  # Stripe types this as a ~300-entry Literal
            connect=False,
            api_version=settings.STRIPE_API_VERSION,
            description="Revel platform events",
        )
        connect = stripe.WebhookEndpoint.create(
            url=url,
            enabled_events=t.cast(t.Any, list(CONNECT_EVENTS)),
            connect=True,
            api_version=settings.STRIPE_API_VERSION,
            description="Revel Connect events",
        )

        if options["format"] == "json":
            self.stdout.write(
                json.dumps(
                    {
                        "platform": {"id": platform.id, "secret": platform.secret},
                        "connect": {"id": connect.id, "secret": connect.secret},
                    }
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("Created two webhook endpoints:"))
        self.stdout.write(f"  Platform: {platform.id}  secret: {platform.secret}")
        self.stdout.write(f"  Connect:  {connect.id}  secret: {connect.secret}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Set in .env (CSV, no spaces — keep the OLD secret during cutover):"))
        self.stdout.write(f"  STRIPE_WEBHOOK_SECRETS={platform.secret},{connect.secret},<old whsec_ during overlap>")

    def _find_pair(self, url: str) -> tuple[t.Any, t.Any]:
        """Return the single (platform, connect) endpoint pair at ``url`` or raise ``CommandError``."""
        # The stripe stubs omit ``connect`` from WebhookEndpoint, though the API returns it.
        existing = [
            t.cast(t.Any, ep) for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter() if ep.url == url
        ]
        platform = [ep for ep in existing if not ep.connect]
        connect = [ep for ep in existing if ep.connect]
        if len(platform) == 1 and len(connect) == 1:
            return platform[0], connect[0]
        found = ", ".join(f"{ep.id} (connect={ep.connect})" for ep in existing) or "none"
        hint = (
            "none: run without --sync to create them"
            if not existing
            else "duplicates: disable the extra ones first; missing: run without --sync to create them"
        )
        raise CommandError(
            f"--sync needs exactly one platform and one Connect endpoint at {url}. Found: {found} ({hint})."
        )

    def _sync(self, url: str, *, dry_run: bool, as_json: bool) -> None:
        """Make the existing platform + Connect endpoints match the canonical event lists."""
        configure_stripe()
        platform, connect = self._find_pair(url)
        plan = [("platform", platform, PLATFORM_EVENTS), ("connect", connect, CONNECT_EVENTS)]
        for _, ep, _canonical in plan:
            if "*" in ep.enabled_events:
                raise CommandError(
                    f'Endpoint {ep.id} subscribes to all events ("*"); refusing to narrow a wildcard. '
                    "Edit it in the Stripe Dashboard if that is intended."
                )

        result: dict[str, dict[str, t.Any]] = {}
        for kind, ep, canonical in plan:
            current, wanted = set(ep.enabled_events), set(canonical)
            added, removed = sorted(wanted - current), sorted(current - wanted)
            result[kind] = {"id": ep.id, "added": added, "removed": removed}
            if (added or removed) and not dry_run:
                # modify replaces the whole list, so send the full canonical one.
                stripe.WebhookEndpoint.modify(ep.id, enabled_events=t.cast(t.Any, list(canonical)))

        if as_json:
            self.stdout.write(json.dumps(result))
        else:
            self._print_sync(result, dry_run=dry_run)

    def _print_sync(self, result: dict[str, dict[str, t.Any]], *, dry_run: bool) -> None:
        """Print the per-endpoint diff in human-readable form."""
        if dry_run:
            self.stdout.write("DRY RUN — no endpoint modified.")
        for kind, info in result.items():
            if not (info["added"] or info["removed"]):
                self.stdout.write(f"{kind} {info['id']}: already in sync")
                continue
            self.stdout.write(f"{kind} {info['id']}: {'would update' if dry_run else 'updated'}")
            for event in info["added"]:
                self.stdout.write(f"  + {event}")
            for event in info["removed"]:
                self.stdout.write(f"  - {event}")
