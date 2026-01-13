"""Event duplication service."""

import typing as t
from contextlib import contextmanager
from datetime import datetime

from django.db import transaction
from django.db.models.signals import post_save

from events.models import Event, PotluckItem, TicketTier


@contextmanager
def _disconnected_event_signal() -> t.Iterator[None]:
    """Context manager to temporarily disconnect the handle_event_save signal.

    This prevents auto-creation of default ticket tiers during event duplication,
    where we want to copy the template's tiers explicitly.
    """
    from events.signals import handle_event_save

    post_save.disconnect(handle_event_save, sender=Event)
    try:
        yield
    finally:
        post_save.connect(handle_event_save, sender=Event)


@transaction.atomic
def duplicate_event(
    template_event: Event,
    new_name: str,
    new_start: datetime,
) -> Event:
    """Create a deep copy of an event with shifted dates.

    All date fields are shifted by the delta between template_event.start and new_start.
    The new event is created in DRAFT status.

    Copies:
    - All event fields (with date shifts)
    - Ticket tiers (with date shifts, reset quantity_sold)
    - Suggested potluck items (is_suggested=True)
    - Tags

    Links to same:
    - Organization questionnaires (M2M)
    - Additional resources (M2M)

    Does NOT copy:
    - Tickets, RSVPs, invitations, tokens, waitlist
    - User-contributed potluck items

    Args:
        template_event: Event to copy from
        new_name: Name for new event
        new_start: Start datetime (anchor for all date shifts)

    Returns:
        New Event in DRAFT status
    """
    delta = new_start - template_event.start

    def shift_date(dt: datetime | None) -> datetime | None:
        return dt + delta if dt else None

    # Calculate new end time (end is required, so template_event.end is always set)
    new_end = template_event.end + delta

    # Disconnect the signal to prevent auto-creation of default ticket tier
    with _disconnected_event_signal():
        new_event = Event.objects.create(
            # FK references (keep same)
            organization=template_event.organization,
            event_series=template_event.event_series,
            city=template_event.city,
            # Overrides
            name=new_name,
            status=Event.EventStatus.DRAFT,
            # Date fields (shifted)
            start=new_start,
            end=new_end,
            rsvp_before=shift_date(template_event.rsvp_before),
            apply_before=shift_date(template_event.apply_before),
            check_in_starts_at=shift_date(template_event.check_in_starts_at),
            check_in_ends_at=shift_date(template_event.check_in_ends_at),
            # All other fields (copied as-is)
            description=template_event.description,
            invitation_message=template_event.invitation_message,
            event_type=template_event.event_type,
            visibility=template_event.visibility,
            max_attendees=template_event.max_attendees,
            waitlist_open=template_event.waitlist_open,
            requires_ticket=template_event.requires_ticket,
            potluck_open=template_event.potluck_open,
            accept_invitation_requests=template_event.accept_invitation_requests,
            can_attend_without_login=template_event.can_attend_without_login,
            address=template_event.address,
            location=template_event.location,
            logo=template_event.logo,
            cover_art=template_event.cover_art,
        )

    # Duplicate ticket tiers
    for tier in template_event.ticket_tiers.all():
        new_tier = TicketTier.objects.create(
            event=new_event,
            name=tier.name,
            visibility=tier.visibility,
            payment_method=tier.payment_method,
            purchasable_by=tier.purchasable_by,
            description=tier.description,
            price=tier.price,
            price_type=tier.price_type,
            pwyc_min=tier.pwyc_min,
            pwyc_max=tier.pwyc_max,
            currency=tier.currency,
            total_quantity=tier.total_quantity,
            quantity_sold=0,  # Reset
            manual_payment_instructions=tier.manual_payment_instructions,
            sales_start_at=shift_date(tier.sales_start_at),
            sales_end_at=shift_date(tier.sales_end_at),
        )
        # Copy M2M for membership tier restrictions
        new_tier.restricted_to_membership_tiers.set(tier.restricted_to_membership_tiers.all())

    # Duplicate suggested potluck items only
    suggested_items = [
        PotluckItem(
            event=new_event,
            name=item.name,
            quantity=item.quantity,
            item_type=item.item_type,
            note=item.note,
            is_suggested=True,
            created_by=None,
            assignee=None,
        )
        for item in template_event.potluck_items.filter(is_suggested=True)
    ]
    if suggested_items:
        PotluckItem.objects.bulk_create(suggested_items)

    # Copy tags
    tag_names = [tag.name for tag in template_event.tags_manager.all()]
    if tag_names:
        new_event.tags_manager.add(*tag_names)

    # Link to same questionnaires
    for oq in template_event.org_questionnaires.all():
        oq.events.add(new_event)

    # Link to same resources
    for resource in template_event.additional_resources.all():
        resource.events.add(new_event)

    return new_event
