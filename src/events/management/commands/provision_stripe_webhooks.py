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

Stripe's WebhookEndpoint object has no ``connect`` attribute (it is only a
create parameter), so ``--sync`` tells the two endpoints apart by, in order: a
``connect`` bool if Stripe ever returns one, the description this command sets
at creation, then whether ``account.updated`` is subscribed (only the Connect
list has it). ``--platform-id`` / ``--connect-id`` pin the pair explicitly.
Disabled endpoints are ignored. Stale event types are REMOVED, so always
preview with ``--dry-run`` first.
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

PLATFORM_DESCRIPTION = "Revel platform events"
CONNECT_DESCRIPTION = "Revel Connect events"
_KINDS = ("platform", "connect")


class EndpointSyncResult(t.TypedDict):
    """Outcome of syncing one endpoint."""

    id: str
    basis: str
    added: list[str]
    removed: list[str]


class SyncResult(t.TypedDict):
    """Outcome of ``--sync`` (also the ``--format json`` payload)."""

    platform: EndpointSyncResult
    connect: EndpointSyncResult
    dry_run: bool


def _endpoints_at(url: str) -> list[t.Any]:
    """Return every Stripe webhook endpoint (any status) whose URL is exactly ``url``."""
    return [t.cast(t.Any, ep) for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter() if ep.url == url]


def _classify(ep: t.Any) -> tuple[str | None, str]:
    """Guess ``("platform"|"connect"|None, basis)`` for an endpoint; ``None`` means undeterminable."""
    flag = getattr(ep, "connect", None)
    if isinstance(flag, bool):
        return ("connect" if flag else "platform"), "connect flag"
    description = getattr(ep, "description", None)
    if description == PLATFORM_DESCRIPTION:
        return "platform", "description"
    if description == CONNECT_DESCRIPTION:
        return "connect", "description"
    events = ep.enabled_events
    if "*" in events:
        return None, "wildcard subscription"
    if "account.updated" in events:
        return "connect", "account.updated subscribed"
    return "platform", "no account.updated subscription"


class Command(BaseCommand):
    """Provision the platform + Connect webhook endpoints in Stripe."""

    help = (
        "Create two Stripe webhook endpoints (platform + Connect) at the given "
        "URL and print the whsec_* signing secrets for STRIPE_WEBHOOK_SECRETS."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire ``--url`` (required), ``--dry-run``, ``--force``, ``--sync``, the id pins and ``--format``."""
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
                "version is NOT changed (it is set only at creation). Stale event types ARE REMOVED, so run "
                "with --dry-run first. Disabled endpoints are ignored. Mutually exclusive with --force."
            ),
        )
        parser.add_argument("--platform-id", help="With --sync: pin the platform endpoint id (we_...).")
        parser.add_argument("--connect-id", help="With --sync: pin the Connect endpoint id (we_...).")
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format. 'json' prints only a machine-readable object (for setup.sh).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Validate flags, then create the pair, preview the creation, or ``--sync`` existing endpoints."""
        url: str = options["url"]
        if not url.startswith("https://"):
            raise CommandError(f"--url must be https:// (Stripe rejects http in live mode). Got: {url}")

        if not options["sync"] and (options["platform_id"] or options["connect_id"]):
            raise CommandError("--platform-id / --connect-id only apply with --sync.")
        if options["sync"]:
            if options["force"]:
                raise CommandError("--sync and --force are mutually exclusive.")
            self._sync(
                url,
                dry_run=options["dry_run"],
                as_json=options["format"] == "json",
                pins={"platform": options["platform_id"], "connect": options["connect_id"]},
            )
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

        existing = _endpoints_at(url)
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

    @staticmethod
    def _resolve_pins(url: str, pins: dict[str, str | None], by_id: dict[str, t.Any]) -> dict[str, tuple[t.Any, str]]:
        """Validate ``--platform-id`` / ``--connect-id``: both must be enabled endpoints at ``url``."""
        pair: dict[str, tuple[t.Any, str]] = {}
        for kind, pin in pins.items():
            if pin is None:
                continue
            if pin not in by_id:
                raise CommandError(f"--{kind}-id {pin} is not a webhook endpoint at {url}.")
            if by_id[pin].status != "enabled":
                raise CommandError(f"--{kind}-id {pin} is disabled.")
            pair[kind] = (by_id[pin], f"--{kind}-id")
        return pair

    def _find_pair(self, url: str, pins: dict[str, str | None]) -> tuple[dict[str, tuple[t.Any, str]], list[t.Any]]:
        """Resolve ``{kind: (endpoint, basis)}`` for the enabled pair at ``url`` plus the ignored disabled ones."""
        endpoints = _endpoints_at(url)
        by_id = {ep.id: ep for ep in endpoints}
        disabled = [ep for ep in endpoints if ep.status != "enabled"]
        pair = self._resolve_pins(url, pins, by_id)
        pinned = {ep.id for ep, _ in pair.values()}
        candidates: dict[str, list[tuple[t.Any, str]]] = {k: [] for k in _KINDS}
        for ep in endpoints:
            if ep.id in pinned or ep.status != "enabled":
                continue
            kind, basis = _classify(ep)
            if kind is None:
                raise CommandError(
                    f"Cannot tell whether {ep.id} is the platform or Connect endpoint ({basis}); "
                    "pin the pair with --platform-id and --connect-id."
                )
            candidates[kind].append((ep, basis))
        for kind in _KINDS:
            if kind in pair:
                continue
            found = candidates[kind]
            label = "Connect" if kind == "connect" else "platform"
            if len(found) > 1:
                ids = ", ".join(ep.id for ep, _ in found)
                raise CommandError(
                    f"duplicate {label} endpoints at {url}: {ids}. Disable the extra ones or pin the right "
                    f"one with --{kind}-id."
                )
            if not found:
                raise CommandError(self._missing_message(kind, label, url, endpoints, disabled))
            pair[kind] = found[0]
        return pair, disabled

    @staticmethod
    def _missing_message(kind: str, label: str, url: str, endpoints: list[t.Any], disabled: list[t.Any]) -> str:
        """Explain why no enabled ``kind`` endpoint was found."""
        if not endpoints:
            return f"No webhook endpoints at {url}: run without --sync to create them."
        off = [ep.id for ep in disabled if _classify(ep)[0] in (kind, None)]
        if off:
            return (
                f"missing {label} endpoint at {url}: only disabled candidate(s) {', '.join(off)}. "
                f"Re-enable one in the Stripe Dashboard or pin another with --{kind}-id."
            )
        return (
            f"missing {label} endpoint at {url} (found: {', '.join(ep.id for ep in endpoints)}). Pin it with "
            f"--{kind}-id if it is mis-detected; otherwise create it in the Stripe Dashboard "
            "(plain provisioning refuses while endpoints exist, and --force creates a duplicate pair)."
        )

    def _sync(self, url: str, *, dry_run: bool, as_json: bool, pins: dict[str, str | None]) -> None:
        """Make the existing platform + Connect endpoints match the canonical event lists."""
        configure_stripe()
        pair, disabled = self._find_pair(url, pins)
        canonical = {"platform": PLATFORM_EVENTS, "connect": CONNECT_EVENTS}
        for ep, _ in pair.values():
            if "*" in ep.enabled_events:
                raise CommandError(
                    f'Endpoint {ep.id} subscribes to all events ("*"); refusing to narrow a wildcard. '
                    "Edit it in the Stripe Dashboard if that is intended."
                )

        def diff(kind: str) -> EndpointSyncResult:
            ep, basis = pair[kind]
            current, wanted = set(ep.enabled_events), set(canonical[kind])
            return {"id": ep.id, "basis": basis, "added": sorted(wanted - current), "removed": sorted(current - wanted)}

        result: SyncResult = {"platform": diff("platform"), "connect": diff("connect"), "dry_run": dry_run}
        if not dry_run:
            self._apply(result, canonical)
        if as_json:
            self.stdout.write(json.dumps(result))
        else:
            self._print_sync(result, [ep.id for ep in disabled])

    @staticmethod
    def _apply(result: SyncResult, canonical: dict[str, tuple[str, ...]]) -> None:
        """Modify each out-of-sync endpoint; on failure report what was applied and what failed."""
        applied: list[str] = []
        for info, kind in ((result["platform"], "platform"), (result["connect"], "connect")):
            if not (info["added"] or info["removed"]):
                continue
            try:
                # modify replaces the whole list, so send the full canonical one.
                stripe.WebhookEndpoint.modify(info["id"], enabled_events=t.cast(t.Any, list(canonical[kind])))
            except Exception as exc:
                raise CommandError(
                    f"Sync failed (applied: {', '.join(applied) or 'none'}; failed: {info['id']}: {exc}). "
                    "Re-run --sync --dry-run to see the remaining diff."
                ) from exc
            applied.append(info["id"])

    def _print_sync(self, result: SyncResult, disabled_ids: list[str]) -> None:
        """Print the per-endpoint diff in human-readable form."""
        dry_run = result["dry_run"]
        if dry_run:
            self.stdout.write("DRY RUN — no endpoint modified.")
        if disabled_ids:
            self.stdout.write(f"ignored (disabled): {', '.join(disabled_ids)}")
        for kind, info in (("platform", result["platform"]), ("connect", result["connect"])):
            head = f"{kind} {info['id']} (matched by {info['basis']})"
            if not (info["added"] or info["removed"]):
                self.stdout.write(f"{head}: already in sync")
                continue
            self.stdout.write(f"{head}: {'would update' if dry_run else 'updated'}")
            for event in info["added"]:
                self.stdout.write(f"  + {event}")
            if info["removed"]:
                verb = "will remove" if dry_run else "removed"
                self.stdout.write(self.style.WARNING(f"  WARNING: {verb} {len(info['removed'])} event type(s):"))
                for event in info["removed"]:
                    self.stdout.write(f"  - {event}")
