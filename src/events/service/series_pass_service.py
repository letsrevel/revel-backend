"""Series pass service: gate, quotes, ticket materialization, and cancellation.

Purchase orchestration lives in ``series_pass_purchase``.
"""

import dataclasses
import functools
import typing as t
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import structlog
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models import F, Q
from django.db.models.deletion import ProtectedError, RestrictedError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SeriesPassCoverageError, SeriesPassHasHoldersError
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    Payment,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.models.ticket import CancellationSource
from events.schema.series_pass import SeriesPassCreateSchema
from events.service import cancellation_service
from events.tasks import materialize_series_pass_holders
from notifications.signals.series_pass import send_series_pass_cancelled, send_series_pass_purchased

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0.00")


@dataclasses.dataclass(frozen=True)
class SeriesPassQuote:
    """Current pro-rata price and purchasability for a series pass."""

    price: Decimal
    passed_events: int
    remaining_events: int
    currency: str
    purchasable: bool
    reason: str | None


def get_quote(series_pass: SeriesPass, now: datetime | None = None) -> SeriesPassQuote:
    """Current pro-rata price and purchasability for a pass. Pure given ``now``."""
    now = now or timezone.now()
    links = series_pass.tier_links.select_related("event")
    passed = sum(1 for link in links if link.event.start < now)
    remaining = links.count() - passed
    price = max(series_pass.price - passed * series_pass.pro_rata_discount, _ZERO).quantize(Decimal("0.01"))

    reason: str | None = None
    if not series_pass.is_active:
        reason = str(_("This pass is not on sale."))
    elif series_pass.sales_start_at and now < series_pass.sales_start_at:
        reason = str(_("Sales have not started yet."))
    elif series_pass.sales_end_at and now > series_pass.sales_end_at:
        reason = str(_("Sales have ended."))
    elif remaining < 2:
        reason = str(_("Not enough remaining events; buy a regular ticket instead."))
    elif series_pass.total_quantity is not None and series_pass.quantity_sold >= series_pass.total_quantity:
        reason = str(_("This pass is sold out."))

    return SeriesPassQuote(
        price=price,
        passed_events=passed,
        remaining_events=remaining,
        currency=series_pass.currency,
        purchasable=reason is None,
        reason=reason,
    )


def pass_visible_to_user(series_pass: SeriesPass, user: RevelUser | AnonymousUser) -> bool:
    """Pass-level visibility, given the series it belongs to is already visible.

    v1 simplification: mirrors ``TicketTier``'s tier-visibility convention (PUBLIC/UNLISTED
    visible to everyone, MEMBERS_ONLY requires an active membership, STAFF_ONLY/PRIVATE are
    staff/owner-only) without the invitation-based branch — ``SeriesPass.clean()`` already
    rejects INVITED/INVITED_AND_MEMBERS, so no invitation-linked visibility exists to replicate.

    Args:
        series_pass: The pass to check (``event_series__organization`` should be
            select_related to avoid an extra query).
        user: The requesting user, possibly anonymous.

    Returns:
        Whether the pass is visible to the user.
    """
    org = series_pass.event_series.organization
    if not user.is_anonymous and (user.is_superuser or user.is_staff or org.is_owner_or_staff(user)):
        return True
    if series_pass.visibility in SeriesPass.Visibility.publicly_accessible():
        return True
    if series_pass.visibility == SeriesPass.Visibility.MEMBERS_ONLY and not user.is_anonymous:
        return OrganizationMember.objects.for_visibility().filter(user=user, organization=org).exists()
    return False


def visible_passes(
    passes: t.Iterable[SeriesPass], org: Organization, user: RevelUser | AnonymousUser
) -> list[SeriesPass]:
    """Filter a list of series passes (all belonging to ``org``) by visibility to ``user``.

    ``pass_visible_to_user`` re-derives the owner/staff and active-membership checks on
    every call; running it per pass over a list would cost up to two extra queries per
    pass. Here both checks are computed once for the whole list.

    Args:
        passes: The passes to filter. Must all belong to ``org``.
        org: The organization the passes belong to.
        user: The requesting user, possibly anonymous.

    Returns:
        The subset of ``passes`` visible to ``user``, in input order.
    """
    is_privileged = not user.is_anonymous and (user.is_superuser or user.is_staff or org.is_owner_or_staff(user))
    is_member = (
        not is_privileged
        and not user.is_anonymous
        and (OrganizationMember.objects.for_visibility().filter(user=user, organization=org).exists())
    )

    def _visible(series_pass: SeriesPass) -> bool:
        if is_privileged:
            return True
        if series_pass.visibility in SeriesPass.Visibility.publicly_accessible():
            return True
        return series_pass.visibility == SeriesPass.Visibility.MEMBERS_ONLY and is_member

    return [series_pass for series_pass in passes if _visible(series_pass)]


class TierLinkInput(t.TypedDict):
    """One (event, tier) pair to link to a SeriesPass."""

    event_id: UUID
    tier_id: UUID


def validate_events_coverable(series: EventSeries, events: t.Sequence[Event]) -> None:
    """Enforce the enable-time coverage gate for a series pass.

    Every event covered by a series pass must be "simple": it must belong to
    the given series (which must itself be non-recurring), be OPEN, require a
    ticket, not be invitation-only, and not be gated by an admission
    questionnaire targeting either the event or the series.

    Args:
        series: The EventSeries the pass belongs to.
        events: The events the pass is meant to cover.

    Raises:
        SeriesPassCoverageError: If the series is recurring, or any event
            fails the coverage gate.
    """
    if series.recurrence_rule_id is not None:
        raise SeriesPassCoverageError(str(_("Series passes are not supported on recurring series.")))
    for event in events:
        if event.event_series_id != series.id:
            raise SeriesPassCoverageError(str(_("Event '%s' does not belong to this series.") % event.name))
        if event.status != Event.EventStatus.OPEN:
            raise SeriesPassCoverageError(str(_("Event '%s' is not open.") % event.name))
        if not event.requires_ticket:
            raise SeriesPassCoverageError(str(_("Event '%s' does not require a ticket.") % event.name))
        if event.visibility == Event.Visibility.PRIVATE:
            raise SeriesPassCoverageError(
                str(_("Event '%s' is invitation-only and cannot be covered by a series pass.") % event.name)
            )
    gated = OrganizationQuestionnaire.objects.filter(
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    ).filter(Q(event_series=series) | Q(events__in=[event.pk for event in events]))
    if gated.exists():
        raise SeriesPassCoverageError(
            str(_("Events gated by an admission questionnaire cannot be covered by a series pass."))
        )


@transaction.atomic
def add_tier_links(
    series_pass: SeriesPass, links: list[TierLinkInput], materialize: bool = True
) -> list[SeriesPassTierLink]:
    """Create and full-clean tier links for a series pass, after the coverage gate.

    Idempotent per (event, tier): re-sending a pair that's already linked with the
    SAME tier is a silent no-op (so re-PUTting an event's ``series_pass_links``, or
    re-POSTing the same extend payload, doesn't 400 the whole request on a unique-
    constraint violation). Re-covering an already-linked event with a DIFFERENT tier
    is rejected instead of silently repointing coverage, which could strand tickets
    already materialized under the old tier.

    Args:
        series_pass: The SeriesPass to attach links to.
        links: Event/tier id pairs to link.
        materialize: When True (default) and links were created, dispatch
            Task 10's Celery materialization so existing ACTIVE holders are
            granted free tickets for the newly-covered events. Pass False for
            the initial-creation path (``create_series_pass``), where there
            are no holders yet.

    Returns:
        The newly-created ``SeriesPassTierLink`` instances, in input order.
        Excludes any input pair that was already linked with the same tier
        (idempotent no-op).

    Raises:
        SeriesPassCoverageError: If any requested event id doesn't exist, any
            covered event fails the coverage gate, or an already-covered event
            is re-linked to a different tier.
        django.core.exceptions.ValidationError: If a link fails model-level
            validation (tier/event/series/currency/seat-mode mismatch).
    """
    requested_event_ids = {link["event_id"] for link in links}
    events = list(Event.objects.filter(pk__in=requested_event_ids))
    if len(events) != len(requested_event_ids):
        raise SeriesPassCoverageError(str(_("One or more events do not exist.")))
    validate_events_coverable(series_pass.event_series, events)

    events_by_id = {event.id: event for event in events}
    existing_tier_by_event_id = dict(
        series_pass.tier_links.filter(event_id__in=requested_event_ids).values_list("event_id", "tier_id")
    )

    created: list[SeriesPassTierLink] = []
    for link in links:
        existing_tier_id = existing_tier_by_event_id.get(link["event_id"])
        if existing_tier_id is not None:
            if existing_tier_id != link["tier_id"]:
                raise SeriesPassCoverageError(
                    str(
                        _("Event '%s' is already covered by this pass via a different tier.")
                        % events_by_id[link["event_id"]].name
                    )
                )
            continue  # Same (event, tier) already linked — idempotent no-op.
        tier_link = SeriesPassTierLink(series_pass=series_pass, event_id=link["event_id"], tier_id=link["tier_id"])
        tier_link.save()  # TimeStampedModel.save() already full_cleans.
        created.append(tier_link)
    if materialize and created:
        event_ids = [str(link.event_id) for link in created]
        transaction.on_commit(lambda: materialize_series_pass_holders.delay(str(series_pass.id), event_ids))
    return created


def create_series_pass(series: EventSeries, payload: SeriesPassCreateSchema) -> SeriesPass:
    """Create a SeriesPass and its tier links in a single transaction.

    Args:
        series: The EventSeries the pass belongs to.
        payload: The admin-supplied create payload; its ``tier_links`` are converted
            to ``TierLinkInput`` pairs via ``tier_links_as_inputs``.

    Returns:
        The created SeriesPass with its tier links attached.

    Raises:
        SeriesPassCoverageError: If the series is recurring or a covered
            event fails the coverage gate.
        django.core.exceptions.ValidationError: If the pass or a tier link
            fails model validation.
    """
    with transaction.atomic():
        series_pass = SeriesPass(event_series=series, **payload.model_dump(exclude={"tier_links"}))
        series_pass.save()  # TimeStampedModel.save() already full_cleans.
        # materialize=False: no holders can exist yet for a pass that is only just being created.
        add_tier_links(series_pass, payload.tier_links_as_inputs, materialize=False)
    return series_pass


def materialize_tickets(
    held_pass: HeldSeriesPass,
    links: t.Sequence[SeriesPassTierLink],
    status: Ticket.TicketStatus,
) -> list[Ticket]:
    """bulk_create one ticket per link, skipping events already ticketed for this pass.

    bulk_create bypasses post_save signals by design — per-ticket notifications must
    never fire for pass tickets (spec §Notifications).

    Args:
        held_pass: The HeldSeriesPass the tickets are materialized for.
        links: The tier links to materialize tickets for.
        status: The status to create the tickets with (ACTIVE for free, PENDING otherwise).

    Returns:
        The created Ticket instances (excludes any skipped as already-ticketed).
    """
    existing_event_ids = set(
        Ticket.objects.filter(held_pass=held_pass)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("event_id", flat=True)
    )
    guest_name = held_pass.user.get_full_name() or held_pass.user.username
    tickets = [
        Ticket(
            event_id=link.event_id,
            tier_id=link.tier_id,
            user=held_pass.user,
            held_pass=held_pass,
            status=status,
            guest_name=guest_name,
            refund_policy_snapshot=None,
        )
        for link in links
        if link.event_id not in existing_event_ids
    ]
    return Ticket.objects.bulk_create(tickets)


def backfill_missing_tickets(held_pass: HeldSeriesPass) -> list[Ticket]:
    """Grant tickets for covered future events the pass missed while PENDING.

    A pass extension (``materialize_series_pass_holders``) only processes ACTIVE
    holders, so a buyer mid-checkout misses any event linked to the pass between
    purchase and activation. Both activation paths (the ``checkout.session.completed``
    webhook and the offline ``confirm_held_pass_payment``) call this to catch up.

    Must run inside a transaction (locks tier rows). Capacity-checked per tier:
    full tiers are skipped and logged, mirroring the extension task.

    Args:
        held_pass: The just-activated pass. Must have ``user`` available.

    Returns:
        The Tickets created (empty if the pass already covers every future event).
    """
    now = timezone.now()
    ticketed_event_ids = set(
        Ticket.objects.filter(held_pass=held_pass)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("event_id", flat=True)
    )
    missing_links = list(
        SeriesPassTierLink.objects.filter(series_pass_id=held_pass.series_pass_id, event__start__gte=now)
        .exclude(event_id__in=ticketed_event_ids)
        .select_related("event")
    )
    if not missing_links:
        return []
    # Lock the mapped tiers in pk order (deadlock discipline, mirrors
    # SeriesPassPurchaseService.purchase and the extension task).
    locked = {
        tier.pk: tier
        for tier in TicketTier.objects.select_for_update()
        .filter(pk__in=[link.tier_id for link in missing_links])
        .order_by("pk")
    }
    grantable: list[SeriesPassTierLink] = []
    for link in missing_links:
        tier = locked[link.tier_id]
        if tier.total_quantity is not None and tier.quantity_sold >= tier.total_quantity:
            logger.info(
                "series_pass_backfill_skipped_full_tier",
                held_pass_id=str(held_pass.id),
                event_id=str(link.event_id),
                tier_id=str(link.tier_id),
            )
            continue
        grantable.append(link)
    created = materialize_tickets(held_pass, grantable, Ticket.TicketStatus.ACTIVE)
    if created:
        # One ticket per tier by construction (each covered event has its own tier link).
        TicketTier.objects.filter(pk__in=[ticket.tier_id for ticket in created]).update(
            quantity_sold=F("quantity_sold") + 1
        )
    return created


def expire_stranded_held_passes(session_ids: t.Collection[str]) -> int:
    """Cancel PENDING held passes whose Stripe checkout session is dead.

    Shared by every online-payment expiry route (the ``cleanup_expired_payments``
    beat task, the resume/cancel-checkout batch cleanup, and the
    ``payment_intent.canceled`` webhook) so an abandoned or expired pass checkout
    never strands the buyer: the pass flips to CANCELLED (freeing the conditional
    unique constraint for a re-purchase) and ``SeriesPass.quantity_sold`` is
    floor-decremented.

    Tier counters are intentionally NOT touched here — each expiry route already
    releases tier capacity per ticket.

    Args:
        session_ids: Stripe checkout session ids whose payments expired/died.

    Returns:
        The number of held passes cancelled.
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return 0
    stranded = list(HeldSeriesPass.objects.filter(stripe_session_id__in=ids, status=HeldSeriesPass.Status.PENDING))
    cancelled = 0
    for held_pass in stranded:
        # Atomic claim: overlapping expiry routes (beat sweep, payment_intent.canceled
        # webhook, user cancel_pending_checkout) can each snapshot the same PENDING
        # pass — only the route that wins this conditional UPDATE may decrement the
        # pass counter, or concurrent claims would double-release it.
        claimed = HeldSeriesPass.objects.filter(pk=held_pass.pk, status=HeldSeriesPass.Status.PENDING).update(
            status=HeldSeriesPass.Status.CANCELLED
        )
        if claimed != 1:
            continue
        cancelled += 1
        SeriesPass.objects.filter(pk=held_pass.series_pass_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )
        logger.info(
            "series_pass_stranded_checkout_expired",
            held_pass_id=str(held_pass.id),
            series_pass_id=str(held_pass.series_pass_id),
            stripe_session_id=held_pass.stripe_session_id,
        )
    return cancelled


def _expire_stripe_session(held_pass: HeldSeriesPass) -> None:
    """Best-effort expiry of the pass's pending Stripe Checkout session.

    Prevents a buyer completing payment on a session whose pass the organizer just
    cancelled. Already-expired/completed sessions raise ``InvalidRequestError`` —
    tolerated (logged): the pass is cancelled either way and the
    ``checkout.session.completed`` handler refuses to resurrect cancelled tickets.
    """
    import stripe
    from django.conf import settings

    expire_kwargs: dict[str, t.Any] = {}
    org_stripe_account = held_pass.series_pass.event_series.organization.stripe_account_id
    if org_stripe_account and org_stripe_account != settings.STRIPE_ACCOUNT:
        expire_kwargs["stripe_account"] = org_stripe_account
    try:
        stripe.checkout.Session.expire(held_pass.stripe_session_id, **expire_kwargs)
    except stripe.error.StripeError as exc:
        logger.warning(
            "series_pass_session_expire_failed",
            held_pass_id=str(held_pass.id),
            stripe_session_id=held_pass.stripe_session_id,
            error=str(exc),
        )


def cancel_held_pass(
    held_pass: HeldSeriesPass,
    cancelled_by: RevelUser,
    reason: str | None = None,
) -> HeldSeriesPass:
    """Organizer-initiated cancellation of an entire series pass.

    Cancels every future, non-checked-in ticket materialized from ``held_pass``;
    past-event and checked-in tickets are left untouched. Refunds any
    successfully-paid ticket (a pass paid online may be mapped to offline/free
    tiers — the Payment row, not the tier's payment method, is authoritative),
    decrements each cancelled ticket's tier ``quantity_sold`` and the pass's own
    ``quantity_sold``, then marks the pass itself CANCELLED. Cancelling an
    already-CANCELLED pass is an idempotent no-op.

    Per-ticket saves in the loop below never emit per-ticket TICKET_CANCELLED
    notifications (``notifications/signals/ticket.py`` gates on ``held_pass_id``);
    instead, a single SERIES_PASS_CANCELLED notification fires to the holder and
    org staff/owners once the cancellation actually commits, carrying the total
    refunded amount and cancelled-ticket count.

    For a PENDING pass with a live Stripe checkout session, the pending Payment
    rows are marked FAILED (so the payment-expiry sweep can't re-release the tier
    capacity this cancellation already freed) and the session is expired at
    Stripe after commit, so a late buyer payment can't slip through.

    Args:
        held_pass: The HeldSeriesPass to cancel.
        cancelled_by: The organizer/staff user performing the cancellation.
        reason: Optional free-text reason recorded on each cancelled ticket.

    Returns:
        The same HeldSeriesPass instance, now with status CANCELLED.

    Note:
        Mirrors ``cancel_ticket_by_user``'s trade-off: Stripe refunds run
        **inside** this transaction, so a Stripe failure rolls back every ticket/tier
        mutation too. Each refund is keyed by ``idempotency_key=f"refund:{ticket.id}"``,
        so a retry cannot double-charge, and Stripe's ``charge.refunded`` webhook
        self-heals the financial state even without one.
    """
    if held_pass.status == HeldSeriesPass.Status.CANCELLED:
        return held_pass

    now = timezone.now()
    refunded_total = _ZERO
    cancelled_count = 0
    with transaction.atomic():
        # Re-read under a row lock and re-check the status: the caller's instance is
        # unlocked, so a concurrent cancel (or expiry route) may have committed
        # CANCELLED after the fast-path check above — cancelling again would
        # double-decrement the pass counter and re-release tier capacity.
        held_pass = (
            HeldSeriesPass.objects.select_for_update(of=("self",))
            .select_related("series_pass", "user")
            .get(pk=held_pass.pk)
        )
        if held_pass.status == HeldSeriesPass.Status.CANCELLED:
            return held_pass
        was_pending = held_pass.status == HeldSeriesPass.Status.PENDING
        # Pass row first, tier rows after (deadlock discipline — see
        # SeriesPassPurchaseService.purchase, the only other writer that locks both).
        held_pass.status = HeldSeriesPass.Status.CANCELLED
        held_pass.save(update_fields=["status"])
        SeriesPass.objects.filter(pk=held_pass.series_pass_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )

        tickets = (
            Ticket.objects.filter(held_pass=held_pass, event__start__gt=now)
            .exclude(status__in=[Ticket.TicketStatus.CANCELLED, Ticket.TicketStatus.CHECKED_IN])
            .select_related("payment", "tier", "event", "event__organization")
            .select_for_update(of=("self",))
        )
        for ticket in tickets:
            payment: Payment | None = getattr(ticket, "payment", None)
            if (
                payment is not None
                and payment.status == Payment.PaymentStatus.SUCCEEDED
                and payment.stripe_payment_intent_id
            ):
                cancellation_service._issue_stripe_refund(ticket, payment, payment.amount, payment.currency)
                refunded_total += payment.amount
            elif payment is not None and payment.status == Payment.PaymentStatus.PENDING:
                # Never-completed checkout: fail the payment so the expiry sweep
                # (which only processes PENDING payments) can't decrement this
                # ticket's tier a second time after we release it below.
                payment.status = Payment.PaymentStatus.FAILED
                payment.save(update_fields=["status"])

            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = now
            ticket.cancelled_by = cancelled_by
            ticket.cancellation_source = CancellationSource.ORGANIZER
            ticket.cancellation_reason = reason or ""
            ticket.save(
                update_fields=[
                    "status",
                    "cancelled_at",
                    "cancelled_by",
                    "cancellation_source",
                    "cancellation_reason",
                ]
            )
            TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            cancelled_count += 1

    if was_pending and held_pass.stripe_session_id:
        _expire_stripe_session(held_pass)

    # Only reached when this call actually performed the cancellation (both early
    # returns above exit the function first), so a repeat cancel can't double-notify.
    transaction.on_commit(
        functools.partial(send_series_pass_cancelled, held_pass.pk, refunded_total, cancelled_count, reason or "")
    )

    return held_pass


def delete_series_pass(series_pass: SeriesPass) -> None:
    """Permanently delete a SeriesPass, provided no non-cancelled holder exists.

    ``HeldSeriesPass.series_pass`` is ``on_delete=PROTECT`` by design, to preserve the
    purchase/attendance audit trail — so even a pass whose only holders are CANCELLED
    can still be undeletable (e.g. a cancelled holder can keep past-event/checked-in
    tickets, which ``cancel_held_pass`` deliberately leaves untouched). Rather than
    force-purging that history, a lingering ``ProtectedError`` is surfaced as the same
    409 the explicit non-cancelled check raises. ``Ticket.held_pass`` is
    ``on_delete=RESTRICT`` instead (not PROTECT), so it can raise the sibling
    ``RestrictedError`` here too — see the field's comment in ``events/models/ticket.py``.

    Args:
        series_pass: The SeriesPass to delete.

    Raises:
        SeriesPassHasHoldersError: If any non-cancelled held pass exists, or deleting
            would violate a protected/restricted FK from historical holder/ticket records.
    """
    if series_pass.held_passes.exclude(status=HeldSeriesPass.Status.CANCELLED).exists():
        raise SeriesPassHasHoldersError(str(_("Cannot delete a series pass with active or pending holders.")))
    try:
        series_pass.delete()
    except (ProtectedError, RestrictedError) as exc:
        raise SeriesPassHasHoldersError(str(_("Cannot delete a series pass with historical holder records."))) from exc


def remove_tier_link(series_pass: SeriesPass, tier_link: SeriesPassTierLink) -> None:
    """Remove a covered event from a SeriesPass, provided no non-cancelled holder exists.

    Removing coverage from a pass already sold to someone would orphan their
    materialized ticket for that event, so it's blocked in v1.

    Args:
        series_pass: The SeriesPass the link belongs to.
        tier_link: The SeriesPassTierLink to remove.

    Raises:
        SeriesPassHasHoldersError: If any held pass (other than CANCELLED) exists.
    """
    if series_pass.held_passes.exclude(status=HeldSeriesPass.Status.CANCELLED).exists():
        raise SeriesPassHasHoldersError(
            str(_("Cannot remove coverage from a series pass with active or pending holders."))
        )
    tier_link.delete()


@transaction.atomic
def confirm_held_pass_payment(held_pass: HeldSeriesPass) -> HeldSeriesPass:
    """Offline-payment confirmation: flip a PENDING held pass and its tickets to ACTIVE.

    Mirrors ``ticket_service.confirm_ticket_payment``'s offline-confirmation role, but
    for a whole series pass: every PENDING ticket materialized from ``held_pass`` (all of
    them, by construction — offline/free purchases never create a mix of statuses) is
    activated alongside the pass itself, then the purchase notification fires (offline
    passes must not notify until the organizer confirms payment).

    Args:
        held_pass: The held pass to confirm. Must have ``series_pass`` selected.

    Returns:
        The same HeldSeriesPass instance, now ACTIVE.

    Raises:
        HttpError 400: If the pass isn't paid OFFLINE, or the held pass isn't PENDING.
    """
    if held_pass.series_pass.payment_method != TicketTier.PaymentMethod.OFFLINE:
        raise HttpError(400, str(_("This series pass is not paid offline.")))

    # Re-read under a row lock and re-check PENDING: the caller's instance is
    # unlocked, so two concurrent confirms (or a confirm/cancel interleave) could
    # both pass an unlocked check and double-send the purchase notification.
    held_pass = (
        HeldSeriesPass.objects.select_for_update(of=("self",))
        .select_related("series_pass", "user")
        .get(pk=held_pass.pk)
    )
    if held_pass.status != HeldSeriesPass.Status.PENDING:
        raise HttpError(400, str(_("This held pass is not pending.")))

    held_pass.status = HeldSeriesPass.Status.ACTIVE
    held_pass.save(update_fields=["status"])
    Ticket.objects.filter(held_pass=held_pass, status=Ticket.TicketStatus.PENDING).update(
        status=Ticket.TicketStatus.ACTIVE
    )
    # Catch up on events linked to the pass while it sat PENDING (the extension
    # task only materializes for ACTIVE holders).
    backfill_missing_tickets(held_pass)
    transaction.on_commit(lambda: send_series_pass_purchased(held_pass.id))
    return held_pass
