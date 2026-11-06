"""Request-scoped purchase workflow for series passes. Mirrors BatchTicketService."""

import typing as t
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.compliance import enforcement as compliance
from events.exceptions import SeriesPassNotPurchasableError
from events.models import (
    Event,
    HeldSeriesPass,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketAttribution,
    TicketTier,
)
from events.service import series_pass_service
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.event_manager import EligibilityService, NextStep, Reasons, UserIsIneligibleError
from events.service.event_manager.gates import QuestionnaireGate
from events.utils.membership_access import is_eligible_member
from notifications.signals.series_pass import send_series_pass_purchased

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema

logger = structlog.get_logger(__name__)


class SeriesPassPurchaseService:
    """Request-scoped workflow: eligibility, all-or-nothing capacity, materialization, payment dispatch."""

    def __init__(self, series_pass: SeriesPass, user: RevelUser) -> None:
        """Initialize the purchase service.

        Args:
            series_pass: The SeriesPass being purchased.
            user: The user purchasing the pass.
        """
        self.series_pass = series_pass
        self.user = user
        self.org = series_pass.event_series.organization

    def _assert_purchasable_by_user(self) -> None:
        if check_user_hard_blacklisted(self.user, self.org):
            raise HttpError(403, str(_("You cannot purchase from this organization.")))
        if self.series_pass.purchasable_by == TicketTier.PurchasableBy.MEMBERS:
            if self.org.is_owner_or_staff(self.user):
                return
            if not is_eligible_member(self.user, self.org.pk):
                raise HttpError(403, str(_("This pass is for members only.")))

    def _assert_series_questionnaires(self) -> None:
        """Enforce the series' admission questionnaires, as a ticket purchase would (#1321).

        The gate runs against the earliest upcoming covered event, scoped to questionnaires
        linked to the series (event-only questionnaires are not a pass-purchase concern).
        Owners and staff bypass; with no upcoming covered event the quote reports the pass
        as not purchasable, so there is nothing to gate.

        Raises:
            UserIsIneligibleError: If a series questionnaire is missing, pending review or failed.
        """
        if self.org.is_owner_or_staff(self.user):
            return
        now = timezone.now()
        accepting = Q(event__status=Event.EventStatus.OPEN) & (
            Q(event__apply_before__isnull=True) | Q(event__apply_before__gte=now)
        )
        # Anchor: earliest event still accepting applications, then any open one, then any upcoming
        # (so the gate never vanishes). One query.
        link = (
            self.series_pass.tier_links.filter(event__start__gte=now)
            .select_related("event")
            .annotate(
                anchor_rank=Case(
                    When(accepting, then=Value(0)),
                    When(event__status=Event.EventStatus.OPEN, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                )
            )
            .order_by("anchor_rank", "event__start")
            .first()
        )
        if link is None:
            return
        service = EligibilityService(self.user, link.event, questionnaire_scope="series")
        # Event invitations never waive the series questionnaire (the gate ignores them in series scope).
        if (result := QuestionnaireGate(service).check()) is None:
            return
        event = link.event
        can_submit = event.status == Event.EventStatus.OPEN and now <= event.effective_apply_deadline
        if result.next_step == NextStep.COMPLETE_QUESTIONNAIRE and not can_submit:
            # The questionnaire is outstanding but no covered event can take a submission any more.
            reason = str(_("The series questionnaire can no longer be submitted, so this pass cannot be purchased."))
            result = result.model_copy(
                update={"reason": reason, "reason_code": Reasons.APPLICATION_DEADLINE_PASSED.code, "next_step": None}
            )
        raise UserIsIneligibleError(str(result.reason), result)

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
        quote = series_pass_service.get_quote(self.series_pass)
        if not quote.purchasable:
            raise SeriesPassNotPurchasableError(quote.reason or str(_("This pass cannot be purchased.")))
        if self._has_active_held_pass():
            raise SeriesPassNotPurchasableError(str(_("You already hold this pass.")))
        self._assert_series_questionnaires()

        # Re-assert the total_quantity cap under a row lock — the quote above checked
        # it unlocked (fast path), so two concurrent buyers could both pass it. Lock
        # order is pass row first, then tiers by pk (deadlock discipline) — every other
        # writer that touches both a SeriesPass and its tiers in one transaction must
        # match it: series_pass_service.cancel_held_pass, and every expiry route that
        # cancels a held pass alongside a tier release — the ticket-keyed
        # series_pass_service.expire_held_passes_for_tickets callers (the
        # cleanup_expired_payments beat task, pending_checkout's
        # _cleanup_expired_batch and cancel_pending_checkout) and the session-keyed
        # series_pass_service.expire_stranded_held_passes caller (stripe_webhooks'
        # handle_payment_intent_canceled).
        locked_pass = SeriesPass.objects.select_for_update().get(pk=self.series_pass.pk)
        if locked_pass.total_quantity is not None and locked_pass.quantity_sold >= locked_pass.total_quantity:
            raise SeriesPassNotPurchasableError(str(_("This pass is sold out.")))

        now = timezone.now()
        future_links: list[SeriesPassTierLink] = list(
            self.series_pass.tier_links.filter(event__start__gte=now).select_related("event").order_by("tier_id")
        )
        if not future_links:
            # Defense-in-depth (#632): get_quote's remaining<2 gate above already
            # blocks this in the normal case, but it and this future_links query each
            # take an independent now() snapshot — a TOCTOU race between the two
            # could in theory let a stale-but-purchasable quote through after every
            # covered event has tipped into the past. Without this guard the ONLINE
            # path would increment quantity_sold and create a PENDING held pass with
            # no tickets and no Payment rows — unreclaimable, since the beat-task
            # cleanup keys off Payment rows.
            raise SeriesPassNotPurchasableError(str(_("This pass has no upcoming events to purchase.")))
        # A paid pass is paid admission to every covered event (EU layer 1, #1057).
        compliance.assert_sale_allowed(
            self.org, self.series_pass.payment_method, [quote.price], [link.event for link in future_links]
        )

        # Lock all mapped tiers in pk order (deadlock discipline, mirrors BatchTicketService).
        locked_tiers = {
            tier.pk: tier
            for tier in TicketTier.objects.select_for_update()
            .filter(pk__in=[link.tier_id for link in future_links])
            .order_by("pk")
        }
        for link in future_links:
            tier = locked_tiers[link.tier_id]
            if tier.total_quantity is not None and tier.quantity_sold >= tier.total_quantity:
                raise HttpError(429, str(_("Event {name} is sold out.")).format(name=link.event.name))

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
