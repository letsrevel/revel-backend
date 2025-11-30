"""Signals for wallet pass updates.

This module listens to changes on Event models and triggers wallet pass
update notifications when relevant fields change (time, location, status, etc.).
"""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from events.models import Event

logger = structlog.get_logger(__name__)


# Fields that should trigger a pass update when changed
PASS_UPDATE_FIELDS = frozenset(
    {
        "name",
        "start",
        "end",
        "address",
        "city_id",
        "location",
        "status",
    }
)


# Store original values for comparison
_original_event_values: dict[t.Any, dict[str, t.Any]] = {}


@receiver(pre_save, sender=Event)
def store_original_event_values(sender: type[Event], instance: Event, **kwargs: t.Any) -> None:
    """Store original field values before save for comparison.

    We need to compare before/after values to determine if a pass-relevant
    field actually changed.
    """
    if not instance.pk:
        # New event, nothing to compare
        return

    try:
        original = Event.objects.get(pk=instance.pk)
        _original_event_values[instance.pk] = {field: getattr(original, field) for field in PASS_UPDATE_FIELDS}
    except Event.DoesNotExist:
        pass


@receiver(post_save, sender=Event)
def trigger_wallet_pass_update(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Trigger wallet pass updates when event details change.

    Only sends notifications if pass-relevant fields actually changed.
    """
    if created:
        # New event - no passes to update yet
        _original_event_values.pop(instance.pk, None)
        return

    original_values = _original_event_values.pop(instance.pk, None)
    if not original_values:
        return

    # Check if any pass-relevant fields changed
    changed_fields = []
    for field in PASS_UPDATE_FIELDS:
        old_value = original_values.get(field)
        new_value = getattr(instance, field)
        if old_value != new_value:
            changed_fields.append(field)

    if not changed_fields:
        return

    logger.info(
        "event_pass_update_triggered",
        event_id=str(instance.id),
        changed_fields=changed_fields,
    )

    # Schedule the update notification task
    def send_update_notifications() -> None:
        from wallet.tasks import send_wallet_update_notifications_for_event

        send_wallet_update_notifications_for_event.delay(str(instance.id))

    transaction.on_commit(send_update_notifications)
