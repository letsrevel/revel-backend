"""Regression: tier fields propagate through event duplication.

Originally added for #370's three new cancellation fields; expanded to cover
other tier configuration fields that were silently dropped by the prior
field-by-field enumeration in ``_duplicate_ticket_tiers``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from events.models.mixins import VisibilityMixin
from events.service.duplication import duplicate_event

pytestmark = pytest.mark.django_db


def test_duplicate_event_copies_all_tier_configuration_fields(event: Event) -> None:
    """All copyable tier fields must survive duplication, not just the originally-listed ones."""
    TicketTier.objects.filter(event=event).delete()
    template_tier = TicketTier.objects.create(
        event=event,
        name="Template Tier",
        price=Decimal("40.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        # visibility/purchasable_by chosen so the two `restrict_*_to_linked_invitations`
        # flags below pass model validation.
        visibility=VisibilityMixin.Visibility.PRIVATE,
        purchasable_by=TicketTier.PurchasableBy.INVITED,
        # #370 fields
        allow_user_cancellation=True,
        cancellation_deadline_hours=24,
        refund_policy={
            "tiers": [{"hours_before_event": 24, "refund_percentage": "50"}],
            "flat_fee": "0",
        },
        # Previously-dropped fields (seat_assignment_mode intentionally omitted here
        # because non-NONE modes require a venue/sector on the tier)
        max_tickets_per_user=3,
        vat_rate=Decimal("21.00"),
        display_order=7,
        restrict_visibility_to_linked_invitations=True,
        restrict_purchase_to_linked_invitations=True,
    )

    new_event = duplicate_event(
        template_event=event,
        new_name="Duplicated Event",
        new_start=event.start + timedelta(weeks=1),
    )

    new_tiers = list(new_event.ticket_tiers.all())
    assert len(new_tiers) == 1
    new_tier = new_tiers[0]

    # #370 fields
    assert new_tier.allow_user_cancellation is True
    assert new_tier.cancellation_deadline_hours == 24
    assert new_tier.refund_policy == template_tier.refund_policy

    # Previously-dropped fields
    assert new_tier.max_tickets_per_user == 3
    assert new_tier.vat_rate == Decimal("21.00")
    assert new_tier.display_order == 7
    assert new_tier.restrict_visibility_to_linked_invitations is True
    assert new_tier.restrict_purchase_to_linked_invitations is True

    # Sanity: per-occurrence state is reset, not copied
    assert new_tier.quantity_sold == 0
    assert new_tier.event_id == new_event.id
