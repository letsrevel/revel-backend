"""Card-dispute handlers for the Stripe webhook dispatcher.

Mixin method for :class:`events.service.stripe_webhooks.StripeEventHandler`.
``charge.dispute.created|updated|closed`` all feed one idempotent upsert keyed
on the Stripe dispute id. Purely operational visibility: the dispute is
recorded next to the payment and the organizer is told about the evidence
deadline and the outcome. Like record-only refunds, a dispute never changes a
ticket or payment.
"""

import typing as t
from datetime import UTC, datetime

import stripe
import structlog
from django.db.models import Q
from django.utils import timezone

from common.utils import get_or_create_with_race_protection
from events.models import Dispute, Organization, Payment
from events.utils.currency import from_stripe_amount
from notifications.signals.payment import send_payment_disputed

logger = structlog.get_logger(__name__)


def _object_id(value: t.Any) -> str:
    """Return the id of a Stripe field that may be an id string or an expanded object."""
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


class DisputeHandlersMixin:
    """``charge.dispute.*`` webhook handler (record + notify, #1245)."""

    def handle_charge_dispute(self, event: stripe.Event) -> None:
        """Upsert the Dispute row for a created/updated/closed event and notify staff.

        Safe under redelivery and out-of-order delivery: the row is keyed on
        ``stripe_dispute_id`` and a terminal status is never overwritten by a
        late, stale non-terminal one. Staff are notified on ``created`` (while
        the dispute is still open) and on ``closed``; ``updated`` is silent.

        Args:
            event: The ``charge.dispute.*`` Stripe event.
        """
        data = event.data.object
        dispute_id = str(data.get("id") or "")
        if not dispute_id:
            logger.warning("stripe_dispute_missing_id", event_id=event.id)
            return

        payment_intent_id = _object_id(data.get("payment_intent"))
        currency = str(data.get("currency") or "").upper()
        due_by = (data.get("evidence_details") or {}).get("due_by")
        fields: dict[str, t.Any] = {
            "stripe_charge_id": _object_id(data.get("charge")),
            "stripe_payment_intent_id": payment_intent_id,
            "amount": from_stripe_amount(int(data.get("amount", 0)), currency),
            "currency": currency,
            "reason": str(data.get("reason") or ""),
            "status": str(data["status"]),
            "evidence_due_by": datetime.fromtimestamp(due_by, tz=UTC) if due_by else None,
        }
        if fields["status"] in Dispute.CLOSED_STATUSES:
            fields["closed_at"] = timezone.now()
        organization = self._resolve_dispute_organization(event, payment_intent_id)

        dispute, created = get_or_create_with_race_protection(
            Dispute,
            Q(stripe_dispute_id=dispute_id),
            {"stripe_dispute_id": dispute_id, "organization": organization, **fields},
        )
        if not created:
            dispute = self._update_dispute(dispute_id, fields, organization, event)
        if dispute.organization_id is None:
            logger.warning("stripe_dispute_unmatched", dispute_id=dispute_id, payment_intent_id=payment_intent_id)

        if event.type == "charge.dispute.created" and not dispute.is_closed:
            send_payment_disputed(dispute=dispute, outcome="opened")
        elif event.type == "charge.dispute.closed":
            send_payment_disputed(dispute=dispute, outcome="closed")

        logger.info("stripe_dispute_recorded", dispute_id=dispute_id, status=dispute.status, created=created)

    @staticmethod
    def _update_dispute(
        dispute_id: str, fields: dict[str, t.Any], organization: Organization | None, event: stripe.Event
    ) -> Dispute:
        """Apply an event's fields to the existing row under a row lock.

        A row already in a terminal status ignores a stale non-terminal update
        (e.g. ``created`` delivered after ``closed``). ``closed_at`` is stamped
        once, on the first transition into a terminal status, and an organization
        resolved late fills a previously unmatched row.
        """
        dispute = Dispute.objects.select_for_update().get(stripe_dispute_id=dispute_id)
        if dispute.is_closed and fields["status"] not in Dispute.CLOSED_STATUSES:
            logger.info("stripe_dispute_stale_event_ignored", dispute_id=dispute_id, event_type=event.type)
            return dispute
        if dispute.closed_at is not None:
            fields = {k: v for k, v in fields.items() if k != "closed_at"}
        for name, value in fields.items():
            setattr(dispute, name, value)
        if dispute.organization_id is None:
            dispute.organization = organization
        dispute.save()
        return dispute

    @staticmethod
    def _resolve_dispute_organization(event: stripe.Event, payment_intent_id: str) -> Organization | None:
        """Resolve the organization from the connected account, else from a matching Payment."""
        account = getattr(event, "account", None)
        if account:
            organization = Organization.objects.filter(stripe_account_id=account).first()
            if organization is not None:
                return organization
        if payment_intent_id:
            payment = (
                Payment.objects.filter(stripe_payment_intent_id=payment_intent_id)
                .select_related("ticket__event__organization")
                .first()
            )
            if payment is not None:
                return payment.ticket.event.organization
        return None
