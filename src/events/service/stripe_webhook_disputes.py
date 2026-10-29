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

from common.models import StripeConnectMixin
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

    if t.TYPE_CHECKING:
        # Provided by the host StripeEventHandler; declared for mypy only.
        @staticmethod
        def _find_connectable(account_id: str) -> StripeConnectMixin | None: ...

    def handle_charge_dispute(self, event: stripe.Event) -> None:
        """Upsert the Dispute row for a created/updated/closed event and notify staff.

        Safe under redelivery and out-of-order delivery: the row is keyed on
        ``stripe_dispute_id`` and locked while updated, and a final status
        (won/lost/prevented) is never overwritten by a stale event.

        Staff get an "opened" alert when a dispute is created, when an inquiry
        escalates into a dispute awaiting a response, and when the organization
        only becomes known after the dispute was first recorded; and a "closed"
        alert when it closes. Other updates are silent.

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

        dispute = Dispute.objects.select_for_update().filter(stripe_dispute_id=dispute_id).first()
        created = dispute is None
        escalated = org_resolved_late = False
        if dispute is None:
            dispute, created = get_or_create_with_race_protection(
                Dispute,
                Q(stripe_dispute_id=dispute_id),
                {
                    "stripe_dispute_id": dispute_id,
                    "organization": self._resolve_dispute_organization(event, payment_intent_id),
                    **self._with_closed_at(fields, None),
                },
            )
            if not created:  # lost a creation race: re-read under lock and apply as an update
                dispute = Dispute.objects.select_for_update().get(pk=dispute.pk)
        if not created:
            previous_status = dispute.status
            organization = None
            if dispute.organization_id is None:
                organization = self._resolve_dispute_organization(event, payment_intent_id)
                org_resolved_late = organization is not None
            applied = self._update_dispute(dispute, fields, organization, event)
            escalated = (
                applied and previous_status in Dispute.WARNING_STATUSES and dispute.status in Dispute.ACTIVE_STATUSES
            )
        if dispute.organization_id is None:
            logger.warning("stripe_dispute_unmatched", dispute_id=dispute_id, payment_intent_id=payment_intent_id)

        # created/updated can both carry an open dispute; closed carries the outcome.
        if event.type == "charge.dispute.closed":
            send_payment_disputed(dispute=dispute, outcome="closed")
        elif not dispute.is_closed and (event.type == "charge.dispute.created" or escalated or org_resolved_late):
            send_payment_disputed(dispute=dispute, outcome="opened")

        logger.info("stripe_dispute_recorded", dispute_id=dispute_id, status=dispute.status, created=created)

    @staticmethod
    def _with_closed_at(fields: dict[str, t.Any], current: datetime | None) -> dict[str, t.Any]:
        """Return ``fields`` with ``closed_at`` set for a closed status (kept once stamped) or cleared on reopening."""
        closed_at = (current or timezone.now()) if fields["status"] in Dispute.CLOSED_STATUSES else None
        return {**fields, "closed_at": closed_at}

    @staticmethod
    def _update_dispute(
        dispute: Dispute, fields: dict[str, t.Any], organization: Organization | None, event: stripe.Event
    ) -> bool:
        """Apply an event's fields to the locked row. Returns False when the event is stale.

        A final status ignores any later non-final event (e.g. ``created``
        delivered after ``closed``), and a full dispute ignores a stale inquiry
        update. A closed inquiry (``warning_closed``) is not final: it can still
        escalate, so that transition is applied and ``closed_at`` cleared. An
        organization resolved late is backfilled even for a stale event.
        """
        if organization is not None:
            dispute.organization = organization
        new_status = fields["status"]
        stale = (dispute.status in Dispute.FINAL_STATUSES and new_status not in Dispute.FINAL_STATUSES) or (
            dispute.status in Dispute.ACTIVE_STATUSES and new_status in Dispute.WARNING_STATUSES
        )
        if stale:
            logger.info(
                "stripe_dispute_stale_event_ignored", dispute_id=dispute.stripe_dispute_id, event_type=event.type
            )
            if organization is not None:
                dispute.save(update_fields=["organization", "updated_at"])
            return False
        for name, value in DisputeHandlersMixin._with_closed_at(fields, dispute.closed_at).items():
            setattr(dispute, name, value)
        dispute.save()
        return True

    def _resolve_dispute_organization(self, event: stripe.Event, payment_intent_id: str) -> Organization | None:
        """Resolve the organization from the connected account, else from a matching Payment."""
        account = getattr(event, "account", None)
        if account:
            connectable = self._find_connectable(account)
            if isinstance(connectable, Organization):
                return connectable
        if payment_intent_id:
            payment = (
                Payment.objects.filter(stripe_payment_intent_id=payment_intent_id)
                .select_related("ticket__event__organization")
                .first()
            )
            if payment is not None:
                return payment.ticket.event.organization
        return None
