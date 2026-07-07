"""Dispatch helpers for series pass notifications.

Unlike most modules in this package, these are plain module-level functions
called explicitly from the purchase service / Stripe webhook handler / Task 10's
Celery materialization task rather than Django signal receivers — mirrors the
``send_batch_ticket_created_notifications`` helper in ``notifications.signals.ticket``.
"""

import typing as t
from uuid import UUID

from events.models import Event, HeldSeriesPass, Ticket
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.signals import notification_requested


def _build_purchased_context(held_pass: HeldSeriesPass) -> dict[str, t.Any]:
    """Build notification context for SERIES_PASS_PURCHASED."""
    series_pass = held_pass.series_pass
    organization = series_pass.event_series.organization
    return {
        "pass_id": str(series_pass.id),
        "pass_name": series_pass.name,
        "series_id": str(series_pass.event_series_id),
        "series_name": series_pass.event_series.name,
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "event_count": held_pass.tickets.exclude(status=Ticket.TicketStatus.CANCELLED).count(),
        "price_paid": str(held_pass.price_paid),
        "currency": series_pass.currency,
    }


def send_series_pass_purchased(held_pass_id: UUID) -> None:
    """Notify the pass holder and org staff/owners that a series pass is now active.

    Call after a ``HeldSeriesPass`` transitions to ACTIVE — either the free
    purchase path (``SeriesPassPurchaseService.purchase``), Stripe webhook
    activation (``handle_checkout_session_completed``), or Task 15's offline
    confirmation endpoint. Offline (PENDING) passes must not trigger this
    until confirmed.

    Args:
        held_pass_id: The id of the now-ACTIVE HeldSeriesPass.
    """
    held_pass = HeldSeriesPass.objects.select_related("series_pass__event_series__organization", "user").get(
        pk=held_pass_id
    )
    context = _build_purchased_context(held_pass)

    notification_requested.send(
        sender=HeldSeriesPass,
        user=held_pass.user,
        notification_type=NotificationType.SERIES_PASS_PURCHASED,
        context=context,
    )

    staff_context = {
        **context,
        "holder_name": held_pass.user.get_display_name(),
        "holder_email": held_pass.user.email,
    }
    organization_id = held_pass.series_pass.event_series.organization_id
    staff_and_owners = get_staff_for_notification(organization_id, NotificationType.SERIES_PASS_PURCHASED)
    for staff_user in staff_and_owners:
        if staff_user.notification_preferences.is_notification_type_enabled(NotificationType.SERIES_PASS_PURCHASED):
            notification_requested.send(
                sender=HeldSeriesPass,
                user=staff_user,
                notification_type=NotificationType.SERIES_PASS_PURCHASED,
                context=staff_context,
            )


def send_series_pass_extended(held_pass_id: UUID, event_ids: list[UUID]) -> None:
    """Notify the pass holder that their pass now covers newly-linked events.

    Called by Task 10's Celery materialization task after granting the holder
    free tickets for events added to a SeriesPass after purchase.

    Args:
        held_pass_id: The id of the extended HeldSeriesPass.
        event_ids: The ids of the newly-covered events.
    """
    held_pass = HeldSeriesPass.objects.select_related("series_pass__event_series__organization", "user").get(
        pk=held_pass_id
    )
    series_pass = held_pass.series_pass
    organization = series_pass.event_series.organization
    new_event_names = list(Event.objects.filter(pk__in=event_ids).order_by("start").values_list("name", flat=True))

    context = {
        "pass_id": str(series_pass.id),
        "pass_name": series_pass.name,
        "series_id": str(series_pass.event_series_id),
        "series_name": series_pass.event_series.name,
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "new_event_count": len(new_event_names),
        "new_event_names": new_event_names,
    }

    notification_requested.send(
        sender=HeldSeriesPass,
        user=held_pass.user,
        notification_type=NotificationType.SERIES_PASS_EXTENDED,
        context=context,
    )
