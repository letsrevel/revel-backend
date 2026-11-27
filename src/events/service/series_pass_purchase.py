"""Request-scoped purchase workflow for series passes. Mirrors BatchTicketService."""

import functools
import typing as t
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra.exceptions import NotFound

from accounts.models import RevelUser
from accounts.service.account_erasure import lock_account_for_checkout
from events.compliance import enforcement as compliance
from events.exceptions import SeriesPassNotPurchasableError
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketAttribution,
    TicketTier,
)
from events.service import series_pass_event_checks, series_pass_service
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.event_manager import UserIsIneligibleError
from events.service.series_pass_questionnaires import check_series_questionnaires
from events.service.series_pass_service import SeriesPassQuote
from events.utils.membership_access import is_eligible_member
from notifications.signals.series_pass import send_series_pass_purchased

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema

logger = structlog.get_logger(__name__)


class SeriesPassPurchaseService:
    """Request-scoped workflow: eligibility, all-or-nothing capacity, materialization, payment dispatch."""

    def __init__(self, series_pass: SeriesPass, user: RevelUser, *, check_visibility: bool = False) -> None:
        """Initialize the purchase service.

        Args:
            series_pass: The SeriesPass being purchased.
            user: The user purchasing the pass.
            check_visibility: Check the checkout route's visibility rule again after the pass row is locked
                (404 if the buyer can no longer see the series or the pass) (#1566). The checkout route sets
                it, because its own check runs without a lock. The default is off so that the service tests,
                which buy with fixtures the buyer cannot see, keep calling the service directly.
        """
        self.series_pass = series_pass
        self.user = user
        self.org = series_pass.event_series.organization
        self.check_visibility = check_visibility

    def _series_still_visible(self, locked_pass: SeriesPass) -> bool:
        """The series part of the checkout route's visibility rule, read after the pass lock wait (#1566).

        ponytail: the series and organization rows are not locked, so a change of theirs that commits after this
        read is not seen (a read after the lock wait still sees what committed during it). It runs under the pass
        lock on purpose, which holds that lock for one more query: correctness over a shorter hold on a busy sale.
        Upgrade: lock the series and organization rows in the global lock order, before the pass.
        """
        return EventSeries.objects.for_user(self.user).filter(pk=locked_pass.event_series_id).exists()

    def _pass_still_visible(self, locked_pass: SeriesPass) -> bool:
        """The pass part of the rule, on the locked row: a change of the pass waits for the lock, so it is exact."""
        return locked_pass.is_active and series_pass_service.pass_visible_to_user(locked_pass, self.user)

    @functools.cached_property
    def _is_owner_or_staff(self) -> bool:
        return self.org.is_owner_or_staff(self.user)

    def _assert_purchasable_by_user(self) -> None:
        if check_user_hard_blacklisted(self.user, self.org):
            raise HttpError(403, str(_("You cannot purchase from this organization.")))
        if self.series_pass.purchasable_by == TicketTier.PurchasableBy.MEMBERS:
            if self._is_owner_or_staff:
                return
            if not is_eligible_member(self.user, self.org.pk):
                raise HttpError(403, str(_("This pass is for members only.")))
        elif self.series_pass.purchasable_by == TicketTier.PurchasableBy.NON_MEMBERS:
            if self.org.is_owner_or_staff(self.user):
                return
            if is_eligible_member(self.user, self.org.pk):
                raise HttpError(403, str(_("This pass is not available to members.")))

    def _assert_series_questionnaires(self) -> None:
        """Enforce the series' admission questionnaires, as a ticket purchase would (#1321).

        Raises:
            UserIsIneligibleError: If a series questionnaire is missing, pending review or failed.
        """
        result = check_series_questionnaires(self.series_pass, self.user, owner_or_staff=self._is_owner_or_staff)
        if result is not None:
            raise UserIsIneligibleError(str(result.reason), result)

    def _lock_tiers_and_find_full(self, future_links: list[SeriesPassTierLink]) -> set[UUID]:
        """Lock all mapped tiers in pk order (deadlock discipline, mirrors BatchTicketService).

        Returns the ids of the covered events whose tier is sold out. The caller raises the 429 only after
        the checks under the event locks, so a covered event that changed answers 409, not "sold out".

        Raises:
            SeriesPassNotPurchasableError: 409 if a covered event (and its tier) was deleted after the links read.
        """
        locked_tiers = {
            tier.pk: tier
            for tier in TicketTier.objects.select_for_update()
            .filter(pk__in=[link.tier_id for link in future_links])
            .order_by("pk")
        }
        if set(locked_tiers) != {link.tier_id for link in future_links}:
            raise SeriesPassNotPurchasableError(str(_("The events of this pass changed. Please try again.")))
        return {
            link.event_id
            for link in future_links
            if (tier := locked_tiers[link.tier_id]).total_quantity is not None
            and tier.quantity_sold >= tier.total_quantity
        }

    def _lock_events_and_assert_unchanged(
        self,
        locked_pass: SeriesPass,
        quote: SeriesPassQuote,
        upcoming_links: list[SeriesPassTierLink],
        future_links: list[SeriesPassTierLink],
        full_tier_event_ids: set[UUID],
    ) -> None:
        """Lock every covered event not yet started, then check the status rules and the price under the locks.

        The cancelled events are locked too: ``update_status`` locks the event row first, so no status
        can change until this purchase commits, and an un-cancel that waits for the lock then
        dispatches its holder materialization after this pass exists (#1496). Under the locks, with a
        new ``now``:

        - the locked quote's remaining events (not started, not cancelled) must be exactly the events
          that get a ticket (``future_links``). A cancel or un-cancel, a deleted event, a start moved,
          an event that started meanwhile or a link added changes that set;
        - for a buyer who is not owner or staff, every non-cancelled event must still be OPEN (the
          ``EventStatusGate`` rule, which ran lock-free before);
        - the quote, computed again from the locked pass row, must still be purchasable, with the same
          price, and the payment method, the buyer rule and the currency must be the ones the purchase
          started with. Visibility is checked in ``purchase()`` after the pass lock, not here (#1566).

        Same capacity counting rules as batch checkout. Must run after the tier locks: batch checkout
        locks tier -> event, so the events are locked last, pk-ordered among themselves.

        Raises:
            SeriesPassNotPurchasableError: 409 if a covered event changed or is gone, a pass field the
                purchase relies on changed, or the pass is no longer purchasable (with the quote's reason).
            HttpError: 429 if a covered event or its tier is at capacity, after the 409 checks.
        """
        locked_events = series_pass_event_checks.lock_events_and_find_full(
            [link.event for link in upcoming_links], self.user
        )
        not_open = {
            event_id
            for event_id, status in locked_events.status.items()
            if status not in (Event.EventStatus.OPEN, Event.EventStatus.CANCELLED)
        }
        locked_quote = series_pass_service.get_quote(locked_pass, timezone.now(), check_event_status=False)
        if not locked_quote.purchasable:
            raise SeriesPassNotPurchasableError(locked_quote.reason or str(_("This pass cannot be purchased.")))
        if (
            locked_quote.upcoming_event_ids != {link.event_id for link in future_links}
            or (not_open and not self._is_owner_or_staff)
            or locked_quote.price != quote.price
            or locked_pass.payment_method != self.series_pass.payment_method
            or locked_pass.purchasable_by != self.series_pass.purchasable_by
            or locked_pass.currency != self.series_pass.currency
        ):
            raise SeriesPassNotPurchasableError(str(_("The events of this pass changed. Please try again.")))
        for link in future_links:
            if link.event_id in full_tier_event_ids or link.event_id in locked_events.full:
                raise HttpError(429, str(_("Event {name} is sold out.")).format(name=link.event.name))

    def _assert_late_links_eligible(
        self, pre_links: list[SeriesPassTierLink], future_links: list[SeriesPassTierLink]
    ) -> None:
        """Gate any event linked between the lock-free gate pass and the locked re-query."""
        gated_event_ids = {link.event_id for link in pre_links}
        if late_links := [link for link in future_links if link.event_id not in gated_event_ids]:
            self._assert_covered_events_eligible(late_links)

    def _assert_covered_events_eligible(self, future_links: list[SeriesPassTierLink]) -> None:
        """Apply the per-event gates of a normal ticket purchase to every covered future event.

        Blacklist (incl. fuzzy name match), event status and full profile. Owners/staff are
        exempt, as in the normal flow's PrivilegedAccessGate. All-or-nothing: runs before any write.

        Raises:
            UserIsIneligibleError: With the first failing verdict (400 + eligibility payload).
        """
        if not self._is_owner_or_staff:
            series_pass_event_checks.assert_events_eligible(self.user, [link.event for link in future_links])

    def _future_links(self, now: datetime) -> "QuerySet[SeriesPassTierLink]":
        """The covered events not yet started, without CANCELLED ones: they get no ticket (#1494)."""
        return self.series_pass.tier_links.filter(event__start__gte=now).exclude(
            event__status=Event.EventStatus.CANCELLED
        )

    def _has_active_held_pass(self) -> bool:
        return (
            HeldSeriesPass.objects.filter(series_pass=self.series_pass, user=self.user)
            .exclude(status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED)
            .exists()
        )

    def _create_held_pass(self, price: Decimal, attribution: TicketAttribution | None) -> HeldSeriesPass:
        """Create the HeldSeriesPass row, mapping a concurrent-purchase race to a 409.

        Two concurrent purchases by the same user can both pass the pre-lock duplicate
        check above. The loser then fails here instead, either via ``full_clean()``
        (usual case: the winner already committed, so ``validate_constraints`` catches
        the conditional unique constraint before the INSERT) or, in the tightest race, a
        raw ``IntegrityError`` from Postgres itself (winner commits between our
        ``full_clean()`` and the INSERT — the transaction is poisoned afterwards, so we
        can't re-query; this table's only unique constraint is the duplicate-pass one,
        so attributing the error to it is safe).
        """
        try:
            return HeldSeriesPass.objects.create(
                series_pass=self.series_pass,
                attribution=attribution,
                user=self.user,
                price_paid=price,
                status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
            )
        except ValidationError as exc:
            if self._has_active_held_pass():
                raise SeriesPassNotPurchasableError(str(_("You already hold this pass."))) from exc
            raise
        except IntegrityError as exc:
            raise SeriesPassNotPurchasableError(str(_("You already hold this pass."))) from exc

    @transaction.atomic
    def purchase(
        self,
        billing_info: "BuyerBillingInfoSchema | None" = None,
        *,
        attribution: TicketAttribution | None = None,
    ) -> HeldSeriesPass | tuple[HeldSeriesPass, UUID]:
        """Purchase the pass.

        ``attribution`` (#922) is stored on the HeldSeriesPass and inherited by every
        ticket materialised for it, now and on later backfills/extensions.

        Returns the HeldSeriesPass (free/offline) or ``(held_pass, reservation_id)``
        for ONLINE passes — the caller must POST the reservation_id to the
        checkout-session endpoint to obtain the Stripe checkout URL (#632).
        """
        self._assert_purchasable_by_user()
        # One ``now`` for the quote and the covered events; the quote under the event locks takes a new one.
        now = timezone.now()
        # The per-event status gate below gives the richer verdict (and exempts owners/staff).
        quote = series_pass_service.get_quote(self.series_pass, now, check_event_status=False)
        if not quote.purchasable:
            raise SeriesPassNotPurchasableError(quote.reason or str(_("This pass cannot be purchased.")))
        if self._has_active_held_pass():
            raise SeriesPassNotPurchasableError(str(_("You already hold this pass.")))
        self._assert_series_questionnaires()

        # Light, lock-free checks first: the covered future events and the per-event gates run before the
        # pass row lock so a refused buyer never holds it. The links are re-queried under the lock below.
        pre_links: list[SeriesPassTierLink] = list(self._future_links(now).select_related("event"))
        self._assert_covered_events_eligible(pre_links)

        # Re-assert the total_quantity cap under a row lock — the quote above checked
        # it unlocked (fast path), so two concurrent buyers could both pass it. Lock
        # order is pass row first, then tiers by pk, then covered events by pk (deadlock
        # discipline; the events last because batch checkout locks tier -> event) — every other
        # writer that touches both a SeriesPass and its tiers in one transaction must
        # match it: series_pass_service.cancel_held_pass, and every expiry route that
        # cancels a held pass alongside a tier release — the ticket-keyed
        # series_pass_service.expire_held_passes_for_tickets callers (the
        # cleanup_expired_payments beat task, pending_checkout's
        # _cleanup_expired_batch and cancel_pending_checkout) and the session-keyed
        # series_pass_service.expire_stranded_held_passes caller (stripe_webhooks'
        # handle_payment_intent_canceled).
        # The buyer's account row comes before every other lock: see lock_account_for_checkout (#1476).
        lock_account_for_checkout(self.user)
        # of=("self",): the joins only feed the quote under the lock below; they are not locked.
        locked_pass = (
            SeriesPass.objects.select_for_update(of=("self",))
            .select_related("event_series__organization__city")
            .get(pk=self.series_pass.pk)
        )
        # Every check before the lock used the series and organization of the first read (#1577). The locked pass
        # row is read again after a lock wait, but the joined series row keeps the statement's snapshot, so the
        # organization is read in a new statement. The series row is not locked: a move that commits after this
        # read is not seen, the same limit as _series_still_visible.
        if (
            locked_pass.event_series_id != self.series_pass.event_series_id
            or EventSeries.objects.filter(pk=locked_pass.event_series_id)
            .values_list("organization_id", flat=True)
            .get()
            != self.org.pk
        ):
            raise SeriesPassNotPurchasableError(str(_("The events of this pass changed. Please try again.")))
        # The checkout route's visibility rule again, after the lock wait (#1566). The lock-free gates above ran on
        # the route's read, so a pass hidden meanwhile can still get their answer (a 400 or 409) first.
        if self.check_visibility and not (
            self._pass_still_visible(locked_pass) and self._series_still_visible(locked_pass)
        ):
            raise NotFound()
        if locked_pass.total_quantity is not None and locked_pass.quantity_sold >= locked_pass.total_quantity:
            raise SeriesPassNotPurchasableError(str(_("This pass is sold out.")))

        # One read of every covered event not yet started, the cancelled ones too: they are locked with the
        # others below, and their status under the lock is compared with this read (#1496).
        upcoming_links: list[SeriesPassTierLink] = list(
            self.series_pass.tier_links.filter(event__start__gte=now).select_related("event").order_by("tier_id")
        )
        future_links = [link for link in upcoming_links if link.event.status != Event.EventStatus.CANCELLED]
        self._assert_late_links_eligible(pre_links, future_links)
        if not future_links:
            # Defense-in-depth (#632): get_quote's remaining<2 gate above already
            # blocks this in the normal case. The quote and this read share one ``now``,
            # but they are two reads: covered events cancelled or moved between them
            # could leave none here. Without this guard the ONLINE
            # path would increment quantity_sold and create a PENDING held pass with
            # no tickets and no Payment rows — unreclaimable, since the beat-task
            # cleanup keys off Payment rows.
            raise SeriesPassNotPurchasableError(str(_("This pass has no upcoming events to purchase.")))
        # A paid pass is paid admission to every covered event (EU layer 1, #1057).
        compliance.assert_sale_allowed(
            self.org, self.series_pass.payment_method, [quote.price], [link.event for link in future_links]
        )

        full_tier_event_ids = self._lock_tiers_and_find_full(future_links)
        self._lock_events_and_assert_unchanged(locked_pass, quote, upcoming_links, future_links, full_tier_event_ids)

        held_pass = self._create_held_pass(quote.price, attribution)
        method = self.series_pass.payment_method
        is_free = method == TicketTier.PaymentMethod.FREE or quote.price <= 0
        ticket_status = Ticket.TicketStatus.ACTIVE if is_free else Ticket.TicketStatus.PENDING
        # Free tickets settle at 0.00 now. A paid OFFLINE ticket's price isn't known
        # per-ticket until confirm_held_pass_payment distributes held_pass.price_paid
        # across them; a paid ONLINE ticket never gets one — Payment.amount is
        # authoritative (#644).
        ticket_price_paid = Decimal("0.00") if is_free else None
        tickets = series_pass_service.materialize_tickets(
            held_pass, future_links, ticket_status, price_paid=ticket_price_paid
        )

        if future_links:
            # Distinct per link by construction (one tier per covered event) — a single
            # UPDATE increments them all instead of one query per link.
            TicketTier.objects.filter(pk__in=[link.tier_id for link in future_links]).update(
                quantity_sold=F("quantity_sold") + 1
            )
        SeriesPass.objects.filter(pk=self.series_pass.pk).update(quantity_sold=F("quantity_sold") + 1)

        logger.info(
            "series_pass_purchase",
            series_pass_id=str(self.series_pass.id),
            held_pass_id=str(held_pass.id),
            user_id=str(self.user.id),
            price=str(quote.price),
            ticket_count=len(tickets),
            payment_method=method,
        )

        if is_free:
            held_pass.status = HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
            held_pass.save(update_fields=["status"])
            # bulk_create skips post_save: rebuild attendee flags for the ACTIVE tickets.
            series_pass_service.rebuild_attendee_visibility(ticket.event_id for ticket in tickets)
            transaction.on_commit(lambda: send_series_pass_purchased(held_pass.id))
            return held_pass
        if method == TicketTier.PaymentMethod.OFFLINE:
            return held_pass
        # ONLINE
        from events.service import stripe_service

        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(
            held_pass=held_pass, tickets=tickets, reservation_id=reservation_id, billing_info=billing_info
        )
        return held_pass, reservation_id
