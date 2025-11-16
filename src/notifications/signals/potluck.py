"""Signal handlers for potluck notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import PotluckItem
from notifications.enums import NotificationType
from notifications.service.eligibility import get_eligible_users_for_event_notification, is_org_staff
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def _get_actor_name(instance: PotluckItem, action: str) -> str | None:
    """Get the name of the person who performed the action."""
    if action == "created" and instance.created_by:
        return instance.created_by.display_name
    if action == "claimed" and instance.assignee:
        return instance.assignee.display_name
    return None


def _build_potluck_context(instance: PotluckItem, action: str, user: RevelUser) -> dict[str, t.Any]:
    """Build notification context for potluck item."""
    event = instance.event
    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    context: dict[str, t.Any] = {
        "potluck_item_id": str(instance.id),
        "item_name": instance.name,
        "event_id": str(event.id),
        "event_name": event.name,
        "action": action,
        "frontend_url": f"{frontend_base_url}/events/{event.id}",
    }

    # Add item details
    context["item_type"] = instance.get_item_type_display()
    if instance.quantity:
        context["quantity"] = instance.quantity
    if instance.note:
        context["note"] = instance.note

    # Add actor name
    actor_name = _get_actor_name(instance, action)
    if actor_name:
        context["actor_name"] = actor_name

    # Check if recipient is organizer
    if event.organization:
        context["is_organizer"] = is_org_staff(user, event.organization)

    return context


@receiver(post_save, sender=PotluckItem)
def handle_potluck_item_save(sender: type[PotluckItem], instance: PotluckItem, created: bool, **kwargs: t.Any) -> None:
    """Handle potluck item creation and updates."""
    event = instance.event

    if created:
        action = "created"
        notification_type = NotificationType.POTLUCK_ITEM_CREATED
        logger.info("potluck_item_created", potluck_item_id=str(instance.id), event_id=str(event.id))
    else:
        # For updates, determine action based on assignee
        if instance.assignee:
            action = "claimed"
            notification_type = NotificationType.POTLUCK_ITEM_CLAIMED
        else:
            action = "unclaimed"
            notification_type = NotificationType.POTLUCK_ITEM_UNCLAIMED
        logger.info("potluck_item_updated", potluck_item_id=str(instance.id), event_id=str(event.id), action=action)

    def send_notifications() -> None:
        eligible_users = get_eligible_users_for_event_notification(event, notification_type)

        for user in eligible_users:
            context = _build_potluck_context(instance, action, user)
            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=notification_type,
                context=context,
            )

    transaction.on_commit(send_notifications)


@receiver(post_delete, sender=PotluckItem)
def handle_potluck_item_delete(sender: type[PotluckItem], instance: PotluckItem, **kwargs: t.Any) -> None:
    """Handle potluck item deletion."""
    logger.info("potluck_item_deleted", potluck_item_id=str(instance.id), event_id=str(instance.event_id))
    event = instance.event

    def send_notifications() -> None:
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_ITEM_DELETED)

        for user in eligible_users:
            context = _build_potluck_context(instance, "deleted", user)
            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.POTLUCK_ITEM_DELETED,
                context=context,
            )

    transaction.on_commit(send_notifications)
