"""Request-scoped purchase workflow for series passes. Mirrors BatchTicketService."""

import typing as t
from decimal import Decimal

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SeriesPassNotPurchasableError
from events.models import HeldSeriesPass, OrganizationMember, SeriesPass, SeriesPassTierLink, Ticket, TicketTier
from events.service import series_pass_service
from events.service.blacklist_service import check_user_hard_blacklisted
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
            is_member = OrganizationMember.objects.active_only().filter(organization=self.org, user=self.user).exists()
            if not is_member:
                raise HttpError(403, str(_("This pass is for members only.")))

    def _has_active_held_pass(self) -> bool:
        return (
            HeldSeriesPass.objects.filter(series_pass=self.series_pass, user=self.user)
            .exclude(status=HeldSeriesPass.Status.CANCELLED)
            .exists()
        )

    def _create_held_pass(self, price: Decimal) -> HeldSeriesPass:
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
                user=self.user,
                price_paid=price,
                status=HeldSeriesPass.Status.PENDING,
            )
        except ValidationError as exc:
            if self._has_active_held_pass():
                raise SeriesPassNotPurchasableError(str(_("You already hold this pass."))) from exc
            raise
        except IntegrityError as exc:
            raise SeriesPassNotPurchasableError(str(_("You already hold this pass."))) from exc

    @transaction.atomic
    def purchase(self, billing_info: "BuyerBillingInfoSchema | None" = None) -> HeldSeriesPass | str:
        """Purchase the pass. Returns checkout URL (online) or the HeldSeriesPass."""
        self._assert_purchasable_by_user()

        quote = series_pass_service.get_quote(self.series_pass)
        if not quote.purchasable:
            raise SeriesPassNotPurchasableError(quote.reason or str(_("This pass cannot be purchased.")))
        if self._has_active_held_pass():
            raise SeriesPassNotPurchasableError(str(_("You already hold this pass.")))

        now = timezone.now()
        future_links: list[SeriesPassTierLink] = list(
            self.series_pass.tier_links.filter(event__start__gte=now).select_related("event").order_by("tier_id")
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

        held_pass = self._create_held_pass(quote.price)
        method = self.series_pass.payment_method
        is_free = method == TicketTier.PaymentMethod.FREE or quote.price <= 0
        ticket_status = Ticket.TicketStatus.ACTIVE if is_free else Ticket.TicketStatus.PENDING
        tickets = series_pass_service.materialize_tickets(held_pass, future_links, ticket_status)

        for link in future_links:
            TicketTier.objects.filter(pk=link.tier_id).update(quantity_sold=F("quantity_sold") + 1)
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
            held_pass.status = HeldSeriesPass.Status.ACTIVE
            held_pass.save(update_fields=["status"])
            transaction.on_commit(lambda: send_series_pass_purchased(held_pass.id))
            return held_pass
        if method == TicketTier.PaymentMethod.OFFLINE:
            return held_pass
        # ONLINE
        from events.service import stripe_service

        return stripe_service.create_series_pass_checkout_session(
            held_pass=held_pass, tickets=tickets, billing_info=billing_info
        )
